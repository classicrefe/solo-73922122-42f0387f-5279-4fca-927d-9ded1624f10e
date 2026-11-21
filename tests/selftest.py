"""自测:核心场景、证据不足分支、增量计数一致性、5 万条变更性能、HTTP 接口。

运行:python3 -m tests.selftest   (在仓库根目录)
"""
import json
import random
import sys
import time
import base64
import io
import zipfile

from app.core import (
    EvidenceError,
    InputError,
    load_receipt_csv,
    load_receipt_csv_allow_empty,
    load_waybill_csv,
    load_waybill_csv_allow_empty,
    parse_evidence,
    verify,
)
from app.backfill import (
    CannotProduce,
    ScopeInsufficient,
    build_output,
    compute_scope,
    parse_approved,
    parse_target_commit,
    run_backfill,
)
from app.backfill_sample import build_backfill_sample
from app.sample import build_sample

FAILED = []


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


def expect_evidence_error(name, fn, needle):
    try:
        fn()
    except EvidenceError as e:
        check(name, needle in str(e), f"-> {e}")
        return
    except Exception as e:  # noqa: BLE001
        check(name, False, f"异常类型不对: {type(e).__name__}: {e}")
        return
    check(name, False, "未抛出 EvidenceError")


def run_verify(evidence_dict, wb_csv, rc_csv):
    ev = parse_evidence(json.loads(json.dumps(evidence_dict)))
    return verify(ev, load_waybill_csv(wb_csv.encode()), load_receipt_csv(rc_csv.encode()))


def t_sample():
    print("== 样例场景(提交 100..110)")
    from app.core import to_intervals
    evidence, wb_csv, rc_csv, expected = build_sample()
    r = run_verify(evidence, wb_csv, rc_csv)
    check("运单 CSV 吻合区间",
          r["waybill_csv"]["intervals"] == to_intervals(expected["waybill_points"]),
          str(r["waybill_csv"]["intervals"]))
    check("签收 CSV 吻合区间",
          r["receipt_csv"]["intervals"] == to_intervals(expected["receipt_points"]),
          str(r["receipt_csv"]["intervals"]))
    check("交集", r["intersection"] == expected["intersection"], str(r["intersection"]))
    check("变更条数", r["changes_applied"] == 12, str(r["changes_applied"]))


def t_counter_consistency():
    print("== 增量计数 vs 全量重算(逐提交核对)")
    from app.core import Engine, parse_evidence as pe
    evidence, wb_csv, rc_csv, _ = build_sample()
    ev = pe(json.loads(json.dumps(evidence)))
    eng = Engine(load_waybill_csv(wb_csv.encode()), load_receipt_csv(rc_csv.encode()))
    eng.load_snapshot(ev.wb_rows, ev.rc_rows)
    ok = (eng.mism_a, eng.mism_b) == eng.full_mismatches()
    for tx in ev.transactions:
        for i, ch in enumerate(tx.changes):
            eng.apply_change(ch, f"t{tx.commit}#{i}")
        ok = ok and (eng.mism_a, eng.mism_b) == eng.full_mismatches()
    check("每个提交后增量计数与全量重算一致", ok)


def t_no_common_source():
    print("== 交集为空 -> 所查范围内没有共同来源")
    evidence = {
        "start_commit": 200, "end_commit": 201,
        "snapshots": {
            "waybills": [{"单号": "W1", "状态": "已签收", "件数": 3}],
            "receipts": [{"签收号": "R1", "单号": "W1", "实收件数": 3}],
        },
        "transactions": [{"commit": 201, "changes": [
            {"table": "waybills", "op": "update", "key": "W1",
             "before": {"单号": "W1", "状态": "已签收", "件数": 3},
             "after": {"单号": "W1", "状态": "已签收", "件数": 4}},
            {"table": "receipts", "op": "update", "key": "R1",
             "before": {"签收号": "R1", "单号": "W1", "实收件数": 3},
             "after": {"签收号": "R1", "单号": "W1", "实收件数": 4}},
        ]}],
    }
    # 运单 CSV 只在 200 吻合;签收 CSV 只在 201 吻合
    r = run_verify(evidence, "单号,状态,件数\nW1,已签收,3\n", "签收号,单号,实收件数\nR1,W1,4\n")
    check("运单吻合点=[200]", r["waybill_csv"]["intervals"] == [[200, 200]], str(r["waybill_csv"]["intervals"]))
    check("签收吻合点=[201]", r["receipt_csv"]["intervals"] == [[201, 201]], str(r["receipt_csv"]["intervals"]))
    check("交集为空", r["intersection"] == [])


def t_evidence_errors():
    print("== 证据不足分支")
    evidence, wb_csv, rc_csv, _ = build_sample()

    gap = json.loads(json.dumps(evidence))
    gap["transactions"] = [t for t in gap["transactions"] if t["commit"] != 105]
    expect_evidence_error("日志缺号(中间缺 105)",
                          lambda: run_verify(gap, wb_csv, rc_csv), "日志缺号")

    trunc = json.loads(json.dumps(evidence))
    trunc["transactions"] = trunc["transactions"][:-2]  # 只到 108,end 仍为 110
    expect_evidence_error("日志缺号(尾部缺失)",
                          lambda: run_verify(trunc, wb_csv, rc_csv), "日志缺号")

    bad_before = json.loads(json.dumps(evidence))
    bad_before["transactions"][2]["changes"][0]["before"]["件数"] = 99  # 提交 103
    expect_evidence_error("前值对不上",
                          lambda: run_verify(bad_before, wb_csv, rc_csv), "前值对不上")

    beyond = json.loads(json.dumps(evidence))
    beyond["end_commit"] = 109  # 日志含 110,超出终止号
    expect_evidence_error("提交号超出终止号",
                          lambda: run_verify(beyond, wb_csv, rc_csv), "超出终止提交号")


def t_receipt_retarget():
    print("== 签收行改挂单号 + 运单状态翻转(多签收行)")
    evidence = {
        "start_commit": 300, "end_commit": 303,
        "snapshots": {
            "waybills": [{"单号": "A", "状态": "已签收", "件数": 1},
                         {"单号": "B", "状态": "运输中", "件数": 2}],
            "receipts": [{"签收号": "R1", "单号": "A", "实收件数": 1},
                         {"签收号": "R2", "单号": "A", "实收件数": 1},
                         {"签收号": "R3", "单号": "B", "实收件数": 2}],
        },
        "transactions": [
            {"commit": 301, "changes": [  # R3 改挂到已签收的 A -> 签收导出多出 R3
                {"table": "receipts", "op": "update", "key": "R3",
                 "before": {"签收号": "R3", "单号": "B", "实收件数": 2},
                 "after": {"签收号": "R3", "单号": "A", "实收件数": 2}}]},
            {"commit": 302, "changes": [  # B 变为已签收,但 B 名下已无签收行
                {"table": "waybills", "op": "update", "key": "B",
                 "before": {"单号": "B", "状态": "运输中", "件数": 2},
                 "after": {"单号": "B", "状态": "已签收", "件数": 2}}]},
            {"commit": 303, "changes": [  # 删除 R3 -> 签收导出恢复吻合
                {"table": "receipts", "op": "delete", "key": "R3",
                 "before": {"签收号": "R3", "单号": "A", "实收件数": 2}, "after": None}]},
        ],
    }
    r = run_verify(evidence, "单号,状态,件数\nA,已签收,1\n",
                   "签收号,单号,实收件数\nR1,A,1\nR2,A,1\n")
    check("运单吻合区间", r["waybill_csv"]["intervals"] == [[300, 301]],
          str(r["waybill_csv"]["intervals"]))
    check("签收吻合区间", r["receipt_csv"]["intervals"] == [[300, 300], [303, 303]],
          str(r["receipt_csv"]["intervals"]))
    check("交集=[300]", r["intersection"] == [[300, 300]], str(r["intersection"]))


def t_csv_validation():
    print("== CSV 输入校验")
    try:
        load_waybill_csv("单号,状态\nW1,已签收\n".encode())
        check("缺列报错", False)
    except InputError as e:
        check("缺列报错", "件数" in str(e), str(e))
    try:
        load_waybill_csv("单号,状态,件数\nW1,已签收,1\nW1,已签收,2\n".encode())
        check("重复键报错", False)
    except InputError as e:
        check("重复键报错", "重复" in str(e), str(e))
    d = load_waybill_csv("单号,状态,件数\nW1,已签收,3\n".encode())
    check("正常解析", d == {"W1": ("已签收", "3")}, str(d))
    d2 = load_waybill_csv(" 单号 ,状态,件数\nW1,已签收,3\n".encode())
    check("表头带空格可解析", d2 == {"W1": ("已签收", "3")}, str(d2))
    d3 = load_waybill_csv("单号,状态,件数\n".encode() and "单号,状态,件数\nW1,已签收,3\n".encode())
    check("BOM 头可解析", load_waybill_csv("单号,状态,件数\nW1,已签收,3\n".encode("utf-8-sig")) == d3)


def t_perf_50k():
    print("== 5 万条变更性能与一致性")
    rng = random.Random(42)
    n_wb, n_rc = 5000, 5000
    wb0 = {f"W{i:05d}": {"单号": f"W{i:05d}", "状态": "已签收" if i % 2 == 0 else "运输中",
                         "件数": rng.randint(1, 9)} for i in range(n_wb)}
    rc0 = {f"R{i:05d}": {"签收号": f"R{i:05d}", "单号": f"W{(i * 7) % n_wb:05d}",
                         "实收件数": rng.randint(1, 9)} for i in range(n_rc)}
    # 工作副本:生成变更日志的同时推进,保证 before 值与重放一致
    wb = {k: dict(v) for k, v in wb0.items()}
    rc = {k: dict(v) for k, v in rc0.items()}
    n_tx, per_tx = 500, 100  # 共 50000 条变更
    transactions = []
    for c in range(1, n_tx + 1):
        changes = []
        for _ in range(per_tx):
            if rng.random() < 0.5:
                k = f"W{rng.randrange(n_wb):05d}"
                before = dict(wb[k])
                after = dict(before)
                if rng.random() < 0.3:
                    after["状态"] = "运输中" if before["状态"] == "已签收" else "已签收"
                else:
                    after["件数"] = rng.randint(1, 9)
                wb[k] = after
                changes.append({"table": "waybills", "op": "update", "key": k,
                                "before": before, "after": after})
            else:
                k = f"R{rng.randrange(n_rc):05d}"
                before = dict(rc[k])
                after = dict(before)
                after["实收件数"] = rng.randint(1, 9)
                rc[k] = after
                changes.append({"table": "receipts", "op": "update", "key": k,
                                "before": before, "after": after})
        transactions.append({"commit": 1000 + c, "changes": changes})
    evidence = {"start_commit": 1000, "end_commit": 1000 + n_tx,
                "snapshots": {"waybills": list(wb0.values()), "receipts": list(rc0.values())},
                "transactions": transactions}

    t0 = time.perf_counter()
    ev = parse_evidence(json.loads(json.dumps(evidence)))
    # 目标取终点状态:最后一个比对点必吻合
    tgt_a = {k: (r["状态"], str(r["件数"])) for k, r in wb.items() if r["状态"] == "已签收"}
    tgt_b = {k: (r["单号"], str(r["实收件数"])) for k, r in rc.items()
             if r["单号"] in tgt_a}
    r = verify(ev, tgt_a, tgt_b)
    dt = time.perf_counter() - t0
    check("变更条数=50000", r["changes_applied"] == 50000, str(r["changes_applied"]))
    check("终点吻合(目标=终点导出)",
          r["waybill_csv"]["intervals"][-1][1] == 1000 + n_tx
          and r["receipt_csv"]["intervals"][-1][1] == 1000 + n_tx,
          f"运单末区间={r['waybill_csv']['intervals'][-1]} 签收末区间={r['receipt_csv']['intervals'][-1]}")
    print(f"  耗时 {dt:.2f}s(解析+重放 50000 条变更, 快照 10000 行)")
    check("性能可接受(<20s)", dt < 20, f"{dt:.2f}s")


def _backfill_inputs():
    ev_dict, wb_csv, rc_csv, demo = build_backfill_sample()
    ev = parse_evidence(json.loads(json.dumps(ev_dict)))
    cur_a = load_waybill_csv_allow_empty(wb_csv.encode())
    cur_b = load_receipt_csv_allow_empty(rc_csv.encode())
    return ev, cur_a, cur_b, demo


def _zip_rows(result):
    raw = base64.b64decode(result["package"]["content_base64"])
    zf = zipfile.ZipFile(io.BytesIO(raw))
    out = {}
    for name in ("waybills.csv", "receipts.csv"):
        text = zf.read(name).decode("utf-8-sig")
        reader = __import__("csv").DictReader(io.StringIO(text))
        out[name] = {row[reader.fieldnames[0]]: tuple(row[c] for c in reader.fieldnames[1:])
                     for row in reader}
    return zf, out


def t_backfill_sample():
    print("== 回补:样例闭包、目标版替换与 ZIP 内容")
    ev, cur_a, cur_b, demo = _backfill_inputs()
    tgt = 505

    r = run_backfill(ev, cur_a, cur_b, tgt, demo["approved"])
    check("回补成功", r["status"] == "ok", r.get("message", ""))
    check("闭包=获准范围", r["scope"]["required"] == demo["approved"],
          str(r["scope"]["required"]))
    zf, rows = _zip_rows(r)
    check("ZIP 仅含两份 CSV", sorted(zf.namelist()) == ["receipts.csv", "waybills.csv"],
          str(zf.namelist()))
    check("运单 CSV 表头沿用导出列",
          zf.read("waybills.csv").decode("utf-8-sig").splitlines()[0] == "单号,状态,件数")
    check("签收 CSV 表头沿用导出列",
          zf.read("receipts.csv").decode("utf-8-sig").splitlines()[0] == "签收号,单号,实收件数")
    check("运单数据(目标版删除 WB101、范围外保留)",
          rows["waybills.csv"] == demo["expected_waybills"], str(rows["waybills.csv"]))
    check("签收数据(改挂落位、删除 RC1001、范围外保留)",
          rows["receipts.csv"] == demo["expected_receipts"], str(rows["receipts.csv"]))

    # 只获准 WB100 -> 必须提示补选 WB101 与签收号 RC1002
    try:
        run_backfill(ev, cur_a, cur_b, tgt, demo["under_scope"])
        check("范围不足时拒绝出包", False, "未抛出 ScopeInsufficient")
    except ScopeInsufficient as e:
        s = e.scope["scope"]
        check("范围不足:列出最少补选单号",
              s["missing"] == demo["under_scope_missing"], str(s["missing"]))
        check("范围不足:列出涉及签收号",
              s["uncovered"][0]["涉及签收号"] == demo["under_scope_receipts"],
              str(s["uncovered"]))
        check("范围不足:结果不含 package", "package" not in e.scope)

    # 起点快照也可作为目标:该票本无变化
    r0 = run_backfill(ev, cur_a, cur_b, 500, ["WB100"])
    check("目标=起点快照可用且无变更",
          r0["status"] == "ok" and r0["stats"]["waybills"]["total_changed"] == 0
          and r0["stats"]["receipts"]["total_changed"] == 0,
          str(r0["stats"]))

    # 目标版新增的运单也可作为获准对象
    r3 = run_backfill(ev, cur_a, cur_b, tgt, ["WB103"])
    _, rows3 = _zip_rows(r3)
    check("目标版新增运单被引入",
          rows3["waybills.csv"].get("WB103") == ("已签收", "1")
          and rows3["receipts.csv"].get("RC1004") == ("WB103", "1"),
          str(rows3["waybills.csv"].get("WB103")))

    # 输出编码/换行跟随下游原文件
    zf_lf = zipfile.ZipFile(io.BytesIO(base64.b64decode(
        run_backfill(ev, cur_a, cur_b, tgt, demo["approved"])["package"]["content_base64"])))
    w_lf = zf_lf.read("waybills.csv")
    check("原文件无 BOM/LF -> 输出一致", not w_lf.startswith(b"\xef\xbb\xbf") and b"\r\n" not in w_lf)
    r_bom = run_backfill(ev, cur_a, cur_b, tgt, demo["approved"],
                         {"bom": True, "crlf": True})
    w_bom = zipfile.ZipFile(io.BytesIO(base64.b64decode(
        r_bom["package"]["content_base64"]))).read("waybills.csv")
    check("原文件有 BOM/CRLF -> 输出一致",
          w_bom.startswith(b"\xef\xbb\xbf") and b"\r\n" in w_bom)


def t_backfill_multihop():
    print("== 回补:多跳闭包(改挂链)与闭包细节")
    tgt_a = {"A": ("已签收", "1"), "B": ("已签收", "1"), "C": ("已签收", "1")}
    tgt_b = {"R1": ("B", "1"), "R2": ("C", "1")}
    cur_a = {"A": ("已签收", "1"), "B": ("已签收", "1"), "C": ("已签收", "1")}
    cur_b = {"R1": ("A", "1"), "R2": ("B", "1")}
    sc = compute_scope(["A"], cur_a, cur_b, tgt_a, tgt_b)
    check("改挂链 A->R1->B->R2->C 全闭包",
          sc["required"] == ["A", "B", "C"] and sc["missing"] == ["B", "C"],
          f"{sc['required']} missing={sc['missing']}")
    by_wb = {u["单号"]: u for u in sc["uncovered"]}
    check("B 的关联签收号(R1 连 A、R2 连 C)", by_wb["B"]["涉及签收号"] == ["R1", "R2"],
          str(by_wb["B"]["涉及签收号"]))
    check("C 的关联签收号", by_wb["C"]["涉及签收号"] == ["R2"],
          str(by_wb["C"]["涉及签收号"]))

    sc2 = compute_scope(["A", "B", "C"], cur_a, cur_b, tgt_a, tgt_b)
    check("闭包全部获准即覆盖", sc2["covered"] and not sc2["missing"])

    out_a, out_b = build_output({"A", "B", "C"}, cur_a, cur_b, tgt_a, tgt_b)
    check("范围内整体取目标版", out_a == tgt_a and out_b == tgt_b,
          f"{out_a} {out_b}")
    extra_a = dict(cur_a, D=("已签收", "1"))
    extra_b = dict(cur_b, R9=("D", "1"))
    out_a2, out_b2 = build_output({"A"}, extra_a, extra_b, tgt_a, tgt_b)
    check("范围外保留旧值", out_a2["D"] == ("已签收", "1")
          and out_b2["R9"] == ("D", "1"), f"{out_a2} {out_b2}")


def t_backfill_gates():
    print("== 回补:出包前置校验")
    ev, cur_a, cur_b, demo = _backfill_inputs()

    # 下游存在无对应运单的签收行 -> 拒绝出包
    bad_b = dict(cur_b)
    bad_b["RC9999"] = ("WB999", "1")
    try:
        run_backfill(ev, cur_a, bad_b, 505, demo["approved"])
        check("签收无运单时拒绝出包", False, "未抛出 CannotProduce")
    except CannotProduce as e:
        check("签收无运单时拒绝出包",
              e.violations and e.violations[0]["签收号"] == "RC9999",
              str(e.violations))
        check("拒绝时结果不含 package", "package" not in e.result)

    # 证据整份校验:任一提交缺号即停止,不出包
    broken = json.loads(json.dumps(build_backfill_sample()[0]))
    broken["transactions"] = [t for t in broken["transactions"] if t["commit"] != 504]
    ev_bad = parse_evidence(broken)
    expect_evidence_error("回补前完整校验证据(缺号)",
                          lambda: run_backfill(ev_bad, cur_a, cur_b, 505,
                                               demo["approved"]), "日志缺号")

    # 目标提交越界
    try:
        run_backfill(ev, cur_a, cur_b, 999, demo["approved"])
        check("目标提交越界报错", False, "未抛出 InputError")
    except InputError as e:
        check("目标提交越界报错", "不在证据范围内" in str(e), str(e))

    # 获准单号不存在
    try:
        run_backfill(ev, cur_a, cur_b, 505, ["NOPE"])
        check("未知获准单号报错", False, "未抛出 InputError")
    except InputError as e:
        check("未知获准单号报错", "NOPE" in str(e), str(e))

    # 目标提交时已退出导出、但整表仍存在的运单可获准(按整票删除处理)
    r = run_backfill(ev, cur_a, cur_b, 505, ["WB102"])
    check("目标版退出的运单可按目标版删除",
          r["scope"]["approved_no_rows"] == []
          and r["stats"]["waybills"]["removed"] == ["WB102"]
          and r["stats"]["receipts"]["removed"] == ["RC1003"], str(r["stats"]))

    check("获准单号解析", parse_approved(" WB100，WB101; WB102\nWB100 ") ==
          ["WB100", "WB101", "WB102"], str(parse_approved("WB100,WB101")))
    check("目标提交号解析", parse_target_commit(" 505 ") == 505)
    try:
        parse_target_commit("505x")
        check("非法目标提交号报错", False)
    except InputError:
        check("非法目标提交号报错", True)

    # 空导出(只有表头)也能回补
    r = run_backfill(ev, {}, {}, 505, ["WB100"])
    check("空下游导出可回补", r["status"] == "ok"
          and r["stats"]["waybills"]["added"] == ["WB100"]
          and r["stats"]["receipts"]["added"] == ["RC1000", "RC1002"], str(r["stats"]))


def t_backfill_http():
    print("== 回补 HTTP 接口(TestClient)")
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] fastapi 不可用: {e}")
        return
    client = TestClient(app)
    ev, wb_csv, rc_csv, demo = build_backfill_sample()
    files = {
        "waybill_csv": ("w.csv", wb_csv.encode(), "text/csv"),
        "receipt_csv": ("r.csv", rc_csv.encode(), "text/csv"),
        "evidence": ("e.json", json.dumps(ev, ensure_ascii=False).encode(),
                     "application/json"),
    }
    resp = client.post("/api/backfill", files=files,
                       data={"target_commit": "505",
                             "approved": "WB100, WB101"})
    body = resp.json()
    check("回补接口 200 ok", resp.status_code == 200 and body["status"] == "ok",
          f"{resp.status_code} {body.get('status')}")
    check("回补接口返回 ZIP(base64)",
          body["package"]["filename"] == "backfill_505.zip"
          and base64.b64decode(body["package"]["content_base64"])[:2] == b"PK")

    resp2 = client.post("/api/backfill", files=files,
                        data={"target_commit": "505", "approved": "WB100"})
    b2 = resp2.json()
    check("范围不足 409 + 补选清单",
          resp2.status_code == 409 and b2["status"] == "insufficient_scope"
          and b2["scope"]["missing"] == ["WB101"], str(b2)[:200])

    resp3 = client.post("/api/backfill", files=files,
                        data={"target_commit": "999", "approved": "WB100"})
    check("目标越界 400", resp3.status_code == 400
          and resp3.json()["status"] == "invalid_input", str(resp3.status_code))

    resp4 = client.post("/api/backfill", files=files,
                        data={"target_commit": "505", "approved": "  "})
    check("空获准范围 400", resp4.status_code == 400, str(resp4.status_code))

    files5 = dict(files)
    files5["evidence"] = ("e.json", b"{not json", "application/json")
    resp5 = client.post("/api/backfill", files=files5,
                        data={"target_commit": "505", "approved": "WB100"})
    check("坏证据包 400", resp5.status_code == 400, str(resp5.status_code))

    page = client.get("/")
    check("页面含回补入口", "运单回补" in page.text and "/api/backfill" in page.text)
    for path in ("/api/backfill/sample/waybills.csv",
                 "/api/backfill/sample/receipts.csv",
                 "/api/backfill/sample/evidence.json"):
        r = client.get(path)
        check(f"回补样例可下载 {path}", r.status_code == 200, str(r.status_code))


def t_http():
    print("== HTTP 接口(TestClient)")
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] fastapi 不可用: {e}")
        return
    client = TestClient(app)
    evidence, wb_csv, rc_csv, expected = build_sample()
    files = {
        "waybill_csv": ("waybills.csv", wb_csv.encode(), "text/csv"),
        "receipt_csv": ("receipts.csv", rc_csv.encode(), "text/csv"),
        "evidence": ("evidence.json", json.dumps(evidence, ensure_ascii=False).encode(),
                     "application/json"),
    }
    resp = client.post("/api/verify", files=files)
    check("样例核查 HTTP 200", resp.status_code == 200, str(resp.status_code))
    body = resp.json()
    check("样例核查结论 ok", body.get("status") == "ok", str(body.get("status")))
    check("样例交集正确", body["result"]["intersection"] == expected["intersection"],
          str(body["result"]["intersection"]))

    bad = json.loads(json.dumps(evidence))
    bad["transactions"] = [t for t in bad["transactions"] if t["commit"] != 107]
    files2 = dict(files)
    files2["evidence"] = ("evidence.json", json.dumps(bad, ensure_ascii=False).encode(),
                          "application/json")
    body2 = client.post("/api/verify", files=files2).json()
    check("缺号 -> insufficient_evidence", body2.get("status") == "insufficient_evidence",
          str(body2.get("status")))

    files3 = dict(files)
    files3["waybill_csv"] = ("waybills.csv", "单号,状态\nW1,已签收\n".encode(), "text/csv")
    resp3 = client.post("/api/verify", files=files3)
    check("坏 CSV -> 400 invalid_input",
          resp3.status_code == 400 and resp3.json().get("status") == "invalid_input",
          f"{resp3.status_code} {resp3.json().get('status')}")

    resp4 = client.get("/")
    check("操作页可访问", resp4.status_code == 200 and "货运数仓来源核对" in resp4.text)
    resp5 = client.get("/api/sample/evidence.json")
    check("样例证据包可下载", resp5.status_code == 200
          and resp5.json()["start_commit"] == 100)


def main():
    t_sample()
    t_counter_consistency()
    t_no_common_source()
    t_evidence_errors()
    t_receipt_retarget()
    t_csv_validation()
    t_perf_50k()
    t_backfill_sample()
    t_backfill_multihop()
    t_backfill_gates()
    t_http()
    t_backfill_http()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {', '.join(FAILED)}")
        sys.exit(1)
    print("全部自测通过。")


if __name__ == "__main__":
    main()
