"""回补演示样例:证据包 + 下游两份导出 CSV + 建议的获准单号。

场景(提交号 500..505,起点快照即提交 500 的导出):

  提交  变更
  500   起点:WB100/RC1000、WB101/RC1001+RC1002、WB102/RC1003、
        WB106/RC1006 均已签收;WB103 运输中
  501   WB101 件数 2->3
  502   RC1002 从 WB101 改挂到 WB100(改挂历史:两版导出中分属不同运单)
  503   WB103 变为已签收,新增签收 RC1004(目标版新增,范围外不引入)
  504   WB102 状态 已签收 -> 异常(目标版导出不再含 WB102/RC1003)
  505   删除 RC1001、删除 WB101(目标版已无 WB101)

下游两份 CSV 等于起点(提交 500)的导出。目标提交 505 的导出中:
  WB100(已签收,2)、WB103(已签收,1)、WB106(已签收,1);
  RC1000(WB100)、RC1002(WB100)、RC1004(WB103)、RC1006(WB106)。

回补闭包:
- 只获准 WB100 时,WB100 名下签收含 RC1002;RC1002 在旧版挂 WB101、
  目标版挂 WB100,因此 WB101 必须一起回补,涉及签收号 RC1002。
- 获准 WB100、WB101 后闭包闭合:WB101 目标版已删除,其运单行与名下
  RC1001 整体删除;WB102/WB106 在范围外,保留旧值;WB103/RC1004 属目标版
  新增且范围外,不引入。

成功结果(范围 WB100、WB101,目标提交 505):
  运单:WB100(已签收,2)、WB102(已签收,2)、WB106(已签收,1)
  签收:RC1000(WB100,2)、RC1002(WB100,2)、RC1003(WB102,2)、RC1006(WB106,1)
"""
from __future__ import annotations

from .core import SIGNED_STATUS

WB_ROWS_0 = [
    {"单号": "WB100", "状态": "已签收", "件数": 2, "目的地": "上海"},
    {"单号": "WB101", "状态": "已签收", "件数": 2, "目的地": "北京"},
    {"单号": "WB102", "状态": "已签收", "件数": 2, "目的地": "天津"},
    {"单号": "WB103", "状态": "运输中", "件数": 1, "目的地": "杭州"},
    {"单号": "WB106", "状态": "已签收", "件数": 1, "目的地": "成都"},
]

RC_ROWS_0 = [
    {"签收号": "RC1000", "单号": "WB100", "实收件数": 2, "签收人": "张三"},
    {"签收号": "RC1001", "单号": "WB101", "实收件数": 2, "签收人": "李四"},
    {"签收号": "RC1002", "单号": "WB101", "实收件数": 2, "签收人": "王五"},
    {"签收号": "RC1003", "单号": "WB102", "实收件数": 2, "签收人": "孙八"},
    {"签收号": "RC1006", "单号": "WB106", "实收件数": 1, "签收人": "赵六"},
]

DOWNSTREAM_WB_CSV = ("单号,状态,件数\n"
                     "WB100,已签收,2\n"
                     "WB101,已签收,2\n"
                     "WB102,已签收,2\n"
                     "WB106,已签收,1\n")
DOWNSTREAM_RC_CSV = ("签收号,单号,实收件数\n"
                     "RC1000,WB100,2\n"
                     "RC1001,WB101,2\n"
                     "RC1002,WB101,2\n"
                     "RC1003,WB102,2\n"
                     "RC1006,WB106,1\n")


def _upd(table, key, before, **changes):
    after = dict(before)
    after.update(changes)
    return {"table": table, "op": "update", "key": key,
            "before": before, "after": after}


def build_backfill_sample():
    """返回 (evidence_dict, waybill_csv_str, receipt_csv_str, demo_dict)。"""
    wb = {r["单号"]: r for r in WB_ROWS_0}
    rc = {r["签收号"]: r for r in RC_ROWS_0}
    w101, w102 = wb["WB101"], wb["WB102"]
    r1001, r1002 = rc["RC1001"], rc["RC1002"]
    new_rc1004 = {"签收号": "RC1004", "单号": "WB103", "实收件数": 1, "签收人": "钱七"}

    transactions = [
        {"commit": 501, "changes": [_upd("waybills", "WB101", w101, 件数=3)]},
        {"commit": 502, "changes": [  # 改挂:RC1002 从 WB101 到 WB100
            {"table": "receipts", "op": "update", "key": "RC1002",
             "before": r1002, "after": dict(r1002, 单号="WB100")}]},
        {"commit": 503, "changes": [
            _upd("waybills", "WB103", wb["WB103"], 状态=SIGNED_STATUS),
            {"table": "receipts", "op": "insert", "key": "RC1004",
             "before": None, "after": new_rc1004}]},
        {"commit": 504, "changes": [_upd("waybills", "WB102", w102, 状态="异常")]},
        {"commit": 505, "changes": [
            {"table": "receipts", "op": "delete", "key": "RC1001",
             "before": r1001, "after": None},
            {"table": "waybills", "op": "delete", "key": "WB101",
             "before": dict(w101, 件数=3), "after": None}]},
    ]
    evidence = {
        "start_commit": 500,
        "end_commit": 505,
        "snapshots": {"waybills": WB_ROWS_0, "receipts": RC_ROWS_0},
        "transactions": transactions,
    }
    demo = {
        "target_commit": 505,
        "approved": ["WB100", "WB101"],
        "approved_text": "WB100, WB101",
        "under_scope": ["WB100"],
        "under_scope_text": "WB100",
        "under_scope_missing": ["WB101"],
        "under_scope_receipts": ["RC1002"],
        "expected_waybills": {
            "WB100": ("已签收", "2"),
            "WB102": ("已签收", "2"),
            "WB106": ("已签收", "1"),
        },
        "expected_receipts": {
            "RC1000": ("WB100", "2"),
            "RC1002": ("WB100", "2"),
            "RC1003": ("WB102", "2"),
            "RC1006": ("WB106", "1"),
        },
    }
    return evidence, DOWNSTREAM_WB_CSV, DOWNSTREAM_RC_CSV, demo
