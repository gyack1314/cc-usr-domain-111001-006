"""完整性报告与落盘输出。

报告严格按轮次（revision）给出结论：

- ``complete_cells``：该轮内未截断取回的最终单元，并集内可以确证没有漏点；
- ``unsolvable_cells``：原子单元（空间单点且时间宽度一）仍被截断，属于
  无法消除的截断，单元不会、也不能被报为完整；
- ``pending_units``：预算耗尽时尚未完成的可续采单元，重启后继续同一会话。
"""
import json
import os
import tempfile

from .cells import Cell

_TERMINAL_STATUS = ("complete", "unsolvable")


def _cell_key(entry):
    return Cell.from_dict(entry["cell"]).key()


def coverage_round(state):
    """返回当前可用于宣告覆盖结论的轮次；没有可用轮次时返回 None。

    优先取最近一个 completed 轮次；否则取 active 轮次（只能给部分结论）。
    已因 revision 过期冻结的轮次不提供覆盖结论。
    """
    for rnd in reversed(state["rounds"]):
        if rnd["status"] == "completed":
            return rnd
    for rnd in reversed(state["rounds"]):
        if rnd["status"] == "active":
            return rnd
    return None


def _terminal_entries(rnd, status):
    entries = []
    for tile in rnd["tiles"]:
        entries.extend(e for e in tile["terminal"] if e["status"] == status)
    entries.sort(key=_cell_key)
    return entries


def _pending_entries(rnd):
    entries = []
    for tile in rnd["tiles"]:
        for cell_dict, chain in tile["queue"]:
            entries.append({
                "tile_index": tile["index"],
                "cell": cell_dict,
                "requests_so_far": len(chain),
            })
    return entries


def _round_summary(rnd):
    complete = sum(
        1 for tile in rnd["tiles"]
        for entry in tile["terminal"] if entry["status"] == "complete"
    )
    unsolvable = sum(
        1 for tile in rnd["tiles"]
        for entry in tile["terminal"] if entry["status"] == "unsolvable"
    )
    pending = sum(len(tile["queue"]) for tile in rnd["tiles"])
    return {
        "round_no": rnd["round_no"],
        "revision": rnd["revision"],
        "status": rnd["status"],
        "budget": rnd["budget"],
        "queries_used": rnd["queries"],
        "complete_cells": complete,
        "unsolvable_cells": unsolvable,
        "pending_units": pending,
        "events": rnd["events"],
    }


def build_report(state):
    rnd = coverage_round(state)
    report = {
        "scope": state["scope"],
        "config": state["config"],
        "rounds": [_round_summary(item) for item in state["rounds"]],
        "coverage_round": None,
        "fully_covered": False,
        "statement": "",
        "complete_cells": [],
        "unsolvable_cells": [],
        "pending_units": [],
        "abandoned_rounds": [
            _round_summary(item) for item in state["rounds"]
            if item["status"] == "abandoned_expired"
        ],
        "catalog": {
            "total_unique_source_ids": len(state["catalog"]),
            "records_in_coverage_revision": 0,
            "records_from_older_revisions": 0,
        },
    }
    if rnd is None:
        report["statement"] = (
            "没有可用的 revision 轮次：所有轮次均已过期或尚未开始，"
            "不能宣告任何区域完整。"
        )
        report["catalog"]["records_from_older_revisions"] = len(state["catalog"])
        return report

    complete = _terminal_entries(rnd, "complete")
    unsolvable = _terminal_entries(rnd, "unsolvable")
    pending = _pending_entries(rnd)
    report["coverage_round"] = rnd["round_no"]
    report["coverage_revision"] = rnd["revision"]
    report["complete_cells"] = complete
    report["unsolvable_cells"] = unsolvable
    report["pending_units"] = pending

    in_revision = sum(
        1 for rec in state["catalog"].values()
        if rec["_harvest"]["round_no"] == rnd["round_no"]
    )
    report["catalog"]["records_in_coverage_revision"] = in_revision
    report["catalog"]["records_from_older_revisions"] = len(state["catalog"]) - in_revision

    fully = (
        rnd["status"] == "completed"
        and not pending
        and not unsolvable
    )
    report["fully_covered"] = fully

    parts = [
        f"覆盖结论基于第 {rnd['round_no']} 轮会话 revision={rnd['revision']}"
        f"（轮次状态：{rnd['status']}）。"
    ]
    if complete:
        parts.append(
            f"已确证采全 {len(complete)} 个最终单元（单元内未发生截断，"
            "单元并集内无漏点），明细见 complete_cells。"
        )
    else:
        parts.append("当前轮次尚无确证采全的单元。")
    if unsolvable:
        missing = sum(e["count"] - len(e["record_ids"]) for e in unsolvable)
        parts.append(
            f"{len(unsolvable)} 个不可解单元存在无法消除的截断：单元已细分到"
            "单个整数空间点且时间宽度为一，同坐标同时刻记录仍超过来源上限，"
            f"至少还有 {missing} 条记录无法通过任何子查询取得，明细见 "
            "unsolvable_cells；这些单元不计入采全区域。"
        )
    if pending:
        parts.append(
            f"{len(pending)} 个未覆盖单元因当轮预算耗尽而暂停（pending_units），"
            "它们是可继续的续采单元；在同一会话内追加预算可继续，"
            "若会话已过期则整体转入新轮次重新采集。"
        )
    if fully:
        parts.append(
            "全部采集范围在该 revision 下确证采全：不存在未覆盖单元，"
            "也不存在不可解截断。"
        )
    elif rnd["status"] == "completed" and not pending:
        parts.append(
            "所有单元均已处理完毕，但存在不可解单元，因此整个范围不能宣告"
            "为完全采全；采全区域以 complete_cells 为准。"
        )
    report["statement"] = "".join(parts)
    return report


def _atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".harvest-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def write_outputs(state, out_dir):
    """写出 state.json、report.json、records.json，返回报告字典。"""
    os.makedirs(out_dir, exist_ok=True)
    report = build_report(state)
    records = sorted(state["catalog"].values(), key=lambda r: r["source_id"])
    _atomic_write(os.path.join(out_dir, "state.json"),
                  json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(os.path.join(out_dir, "report.json"),
                  json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(os.path.join(out_dir, "records.json"),
                  json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    return report
