"""回补引擎。

在「来源核对」的重放状态机之上,按目标提交取两表导出视图,把下游两份导出
CSV 中被获准运单覆盖的部分整体替换为目标版本,生成可下载的 ZIP。

核心规则
--------
- 回补单元是「一票运单」:运单行 + 该单号名下(旧版或目标版)的全部签收明细,
  整体换成目标版;目标版已不存在的行即删除。
- 若同一签收号在旧版与目标版挂到不同运单,则新旧两票都被卷入(闭包),以免
  签收明细只换一半;闭包会继续沿新卷入运单的签收明细扩展,直到不动点。
- 获准单号必须覆盖上述闭包,否则不生成包,并列出最少还需补选的运单号及其
  与已覆盖范围发生关联的签收号。
- 范围外的行原样保留,不读取目标版本的值。
- 出包前再校验:回补后的每条签收明细都必须能对应到一条运单行。
"""
from __future__ import annotations

import base64
import csv
import io
import re
import zipfile

from .core import (
    Evidence,
    InputError,
    RC_KEY,
    RC_VALUE_COLS,
    WB_KEY,
    WB_VALUE_COLS,
    replay_export_view,
)

# 导出/回补包使用的固定列(与现有导出列一致)
WB_COLUMNS = (WB_KEY,) + WB_VALUE_COLS
RC_COLUMNS = (RC_KEY,) + RC_VALUE_COLS
ZIP_WAYBILL_NAME = "waybills.csv"
ZIP_RECEIPT_NAME = "receipts.csv"


class ScopeInsufficient(Exception):
    """获准范围不足以覆盖必须一起回补的运单闭包。"""

    def __init__(self, scope: dict):
        super().__init__("获准范围不足")
        self.scope = scope


class CannotProduce(Exception):
    """范围与证据都通过,但回补结果不满足完整性要求,不能出包。"""

    def __init__(self, violations: list, result: dict | None = None):
        super().__init__("回补结果不完整")
        self.violations = violations
        self.result = result or {}


def parse_target_commit(value) -> int:
    if isinstance(value, bool):
        raise InputError(f"目标提交号必须是整数,实际为 {value!r}")
    if isinstance(value, int):
        return value
    text = str(value if value is not None else "").strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        raise InputError(f"目标提交号必须是整数,实际为 {value!r}")
    return int(text)


def parse_approved(text) -> list:
    """解析获准单号:逗号(中英文)、顿号、分号、竖线、空白或换行分隔;去重排序。"""
    if text is None:
        return []
    parts = re.split(r"[,，、;；|\s]+", str(text).strip())
    return sorted({p for p in parts if p})


# ---- 范围闭包 ----

def _index_exports(cur_a: dict, cur_b: dict, tgt_a: dict, tgt_b: dict) -> dict:
    """建立运单/签收的关联索引。"""
    old_by_wb, new_by_wb = {}, {}
    for r, (wb, _n) in cur_b.items():
        old_by_wb.setdefault(wb, set()).add(r)
    for r, (wb, _n) in tgt_b.items():
        new_by_wb.setdefault(wb, set()).add(r)

    def wbs_of_receipt(r):
        out = set()
        if r in cur_b:
            out.add(cur_b[r][0])
        if r in tgt_b:
            out.add(tgt_b[r][0])
        return out

    def receipts_of_wb(wb):
        return old_by_wb.get(wb, set()) | new_by_wb.get(wb, set())

    return {
        "old_by_wb": old_by_wb,
        "new_by_wb": new_by_wb,
        "wbs_of_receipt": wbs_of_receipt,
        "receipts_of_wb": receipts_of_wb,
        "known_waybills": (set(cur_a) | set(tgt_a)
                           | {wb for wb, _n in cur_b.values()}
                           | {wb for wb, _n in tgt_b.values()}),
    }


def compute_scope(approved: list, cur_a: dict, cur_b: dict,
                  tgt_a: dict, tgt_b: dict, known_waybills=None) -> dict:
    """计算必须一起回补的运单闭包,并与获准范围比较。

    闭包从获准单号出发:取该票在旧版与目标版名下的全部签收行,再取这些签收号
    在旧版与目标版挂靠的全部运单,反复扩展至不动点。这样「同一签收号在两版
    导出中挂到不同运单」以及连带发生的改挂都会被整票卷入。

    known_waybills 给出「有效但当前不在导出里」的单号(如目标版中状态非已签收
    的运单);这类获准单号按空操作处理,只有完全未知的单号才报输入错误。

    返回 scope 字典:
      approved / required / missing: 排序后的单号列表;
      uncovered: 每个缺失单号的关联签收号明细(为什么必须补选);
      covered: 获准范围是否已覆盖闭包;
      approved_no_rows: 获准但在两版导出中都没有任何行的单号(不产生变更)。
    """
    idx = _index_exports(cur_a, cur_b, tgt_a, tgt_b)
    known = idx["known_waybills"] | set(known_waybills or ())
    unknown = [w for w in approved if w not in known]
    if unknown:
        raise InputError(
            "获准单号在下游导出和目标版本中都不存在: " + ", ".join(unknown))

    required = set(approved)
    queue = list(approved)
    # BFS:新卷入的运单继续展开名下签收,直到不动点
    while queue:
        wb = queue.pop()
        for r in idx["receipts_of_wb"](wb):
            for other in idx["wbs_of_receipt"](r):
                if other not in required:
                    required.add(other)
                    queue.append(other)

    missing = sorted(required - set(approved))
    uncovered = []
    for wb in missing:
        links = []
        for r in sorted(idx["receipts_of_wb"](wb)):
            others = sorted(idx["wbs_of_receipt"](r) & required - {wb})
            if not others:
                continue
            old_wb = cur_b.get(r, (None,))[0]
            new_wb = tgt_b.get(r, (None,))[0]
            links.append({
                "签收号": r,
                "原运单": old_wb,
                "目标运单": new_wb,
                "关联单号": others,
                "关联已获准": bool(set(others) & set(approved)),
            })
        uncovered.append({
            "单号": wb,
            "涉及签收号": sorted({link["签收号"] for link in links}),
            "关联明细": links,
        })

    no_rows = sorted(
        w for w in set(approved)
        if w not in cur_a and w not in tgt_a and not idx["receipts_of_wb"](w))

    return {
        "approved": sorted(set(approved)),
        "required": sorted(required),
        "missing": missing,
        "uncovered": uncovered,
        "covered": not missing,
        "approved_no_rows": no_rows,
    }


# ---- 结果构造 ----

def build_output(scope_wbs: set, cur_a: dict, cur_b: dict,
                 tgt_a: dict, tgt_b: dict) -> tuple:
    """范围外的行保留旧值,范围内的行取自目标版本(目标版没有即删除)。

    闭包已保证:凡与范围内运单通过签收号相连的运单都在 scope_wbs 中,因此
    「旧版挂靠范围内运单」的签收行在目标版只会挂靠范围内运单或已删除,按
    目标版重建不会丢失或错挂。
    """
    out_a = {k: v for k, v in cur_a.items() if k not in scope_wbs}
    out_a.update({k: v for k, v in tgt_a.items() if k in scope_wbs})

    out_b = {k: v for k, v in cur_b.items() if v[0] not in scope_wbs}
    out_b.update({k: v for k, v in tgt_b.items() if v[0] in scope_wbs})
    return out_a, out_b


def _check_receipts_have_waybills(out_a: dict, out_b: dict) -> list:
    violations = []
    for r in sorted(out_b):
        wb = out_b[r][0]
        if wb not in out_a:
            violations.append({
                "签收号": r,
                "单号": wb,
                "说明": "回补后签收明细找不到对应运单行",
            })
    return violations


def _render_csv(columns, rows: dict, bom: bool = False,
                crlf: bool = False) -> bytes:
    """按导出列渲染 CSV。

    编码与换行风格跟随上传的下游 CSV:BOM 与 CRLF 只在原文件如此时保留,
    避免下游导入对首列/行尾的差异敏感。
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n" if crlf else "\n")
    writer.writerow(columns)
    for key in sorted(rows):
        writer.writerow((key,) + tuple(rows[key]))
    return buf.getvalue().encode("utf-8-sig" if bom else "utf-8")


def make_zip(waybill_bytes: bytes, receipt_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in ((ZIP_WAYBILL_NAME, waybill_bytes),
                           (ZIP_RECEIPT_NAME, receipt_bytes)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def _diff_stats(cur: dict, out: dict) -> dict:
    added = sorted(set(out) - set(cur))
    removed = sorted(set(cur) - set(out))
    changed = sorted(k for k in set(cur) & set(out) if cur[k] != out[k])
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "total_changed": len(added) + len(removed) + len(changed),
    }


def csv_style(raw: bytes) -> dict:
    """探测上传 CSV 的 BOM 与换行风格,用于回补输出保持一致。"""
    return {
        "bom": raw.startswith(b"\xef\xbb\xbf"),
        "crlf": b"\r\n" in raw[:4096],
    }


def run_backfill(ev: Evidence, cur_a: dict, cur_b: dict,
                 target_commit: int, approved: list,
                 output_style: dict | None = None) -> dict:
    """执行回补校验与结果构造。

    output_style 可选 {"bom": bool, "crlf": bool},用于让输出 CSV 的编码/换行
    与下游原文件一致;缺省为无 BOM、LF。

    证据无效抛 EvidenceError;输入不合法抛 InputError;范围不足抛
    ScopeInsufficient;结果不完整抛 CannotProduce。成功返回可序列化结果,
    其中 package 含 ZIP 文件名与 base64 内容。
    """
    # 先完整重放证据(校验整份日志),再取目标提交完成后的导出视图
    target = replay_export_view(ev, target_commit)
    tgt_a, tgt_b = target["waybills"], target["receipts"]
    # 目标提交时整表中有、但导出视图中没有的运单(如状态非已签收)也算已知单号
    known_wbs = set(target["waybills_table"])
    known_wbs.update(r.get("单号") for r in target["receipts_table"].values())

    scope = compute_scope(approved, cur_a, cur_b, tgt_a, tgt_b, known_wbs)
    base = {
        "target_commit": target_commit,
        "evidence": {
            "start_commit": ev.start_commit,
            "end_commit": ev.end_commit,
            "changes_applied": ev.total_changes,
            "snapshot_rows": {"waybills": len(ev.wb_rows),
                              "receipts": len(ev.rc_rows)},
        },
        "scope": scope,
        "input_rows": {"waybills": len(cur_a), "receipts": len(cur_b)},
        "target_rows": {"waybills": len(tgt_a), "receipts": len(tgt_b)},
    }
    if not scope["covered"]:
        result = dict(base, status="insufficient_scope",
                      message=(f"获准范围不足:至少还要补选 {len(scope['missing'])} "
                               "票运单才能整体回补"))
        raise ScopeInsufficient(result)

    out_a, out_b = build_output(set(scope["required"]),
                                cur_a, cur_b, tgt_a, tgt_b)
    violations = _check_receipts_have_waybills(out_a, out_b)
    if violations:
        raise CannotProduce(violations, dict(
            base, status="cannot_produce",
            message="回补后存在没有对应运单的签收明细"))

    style = output_style or {}
    bom, crlf = bool(style.get("bom")), bool(style.get("crlf"))
    waybill_bytes = _render_csv(WB_COLUMNS, out_a, bom, crlf)
    receipt_bytes = _render_csv(RC_COLUMNS, out_b, bom, crlf)
    zip_bytes = make_zip(waybill_bytes, receipt_bytes)

    wa = _diff_stats(cur_a, out_a)
    rc = _diff_stats(cur_b, out_b)
    filename = f"backfill_{target_commit}.zip"
    result = dict(
        base,
        status="ok",
        message=(f"范围覆盖完整,证据有效,回补后每条签收均有对应运单;"
                 f"已生成 {filename}"),
        stats={
            "waybills": wa,
            "receipts": rc,
            "output_rows": len(out_a) + len(out_b),
        },
        package={
            "filename": filename,
            "content_base64": base64.b64encode(zip_bytes).decode("ascii"),
            "files": [ZIP_WAYBILL_NAME, ZIP_RECEIPT_NAME],
            "size_bytes": len(zip_bytes),
        },
    )
    return result
