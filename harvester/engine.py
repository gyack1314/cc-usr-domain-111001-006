"""分片采集引擎：预算、revision 轮次、断点续采与来源编号去重。

状态文件（JSON）结构：

    {
      "version": 1,
      "scope": {单元},
      "config": {"limit": 3, "tiles": N},
      "rounds": [
        {
          "round_no": 1, "revision": "r1",
          "status": "active|completed|abandoned_expired",
          "budget": 本轮累计请求额度, "queries": 已用请求数,
          "events": [{"type": "expired", ...}],
          "tiles": [
            {"index": 0, "root": {单元},
             "queue": [[{单元}, [请求记录...]], ...],
             "terminal": [{"cell", "status", "count", "requests", "record_ids"}]}
          ]
        }
      ],
      "catalog": {source_id: 业务记录 + _harvest 溯源信息}
    }

不变量：

1. 每个 tile 的 queue + terminal 中的单元构成该 tile 根单元的一个互不重叠
   划分（二分拆分保证），因此所有 complete 单元并集内不存在漏点。
2. 一个轮次内所有请求使用同一 revision；收到 410 后轮次立即冻结为
   abandoned_expired，新会话开启全新轮次，覆盖结论绝不跨 revision 拼接。
3. catalog 以 source_id 去重；较新轮次（revision 更新）的记录覆盖较旧
   记录，同一轮次重复见到同一 source_id 只追加溯源、不制造重复资料。
"""
import json
import os
import tempfile

from .cells import Cell, make_tiles, split_cell
from .client import SessionExpired

STATE_VERSION = 1


class HarvestEngine:
    def __init__(self, client, state_path, scope, budget, tiles=4, limit=3):
        if budget < 1:
            raise ValueError("budget 必须 >= 1")
        if tiles < 1:
            raise ValueError("tiles 必须 >= 1")
        self.client = client
        self.state_path = state_path
        self.scope = scope
        self.budget = int(budget)
        self.tiles = int(tiles)
        self.limit = int(limit)
        self.state = self._load_or_init()

    # ------------------------------------------------------------------ 状态
    def _load_or_init(self):
        if os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            if state.get("version") != STATE_VERSION:
                raise ValueError("状态文件版本不兼容")
            saved_scope = Cell.from_dict(state["scope"])
            if saved_scope != self.scope:
                raise ValueError(
                    "状态文件中的采集范围与本次指定不一致，拒绝混用："
                    f"{saved_scope} != {self.scope}"
                )
            if state["config"]["limit"] != self.limit:
                raise ValueError("limit 与状态文件不一致")
            return state
        return {
            "version": STATE_VERSION,
            "scope": self.scope.to_dict(),
            "config": {"limit": self.limit, "tiles": self.tiles},
            "rounds": [],
            "catalog": {},
        }

    def _save(self):
        directory = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".harvest-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, self.state_path)
        except BaseException:
            os.unlink(tmp)
            raise

    # ------------------------------------------------------------------ 轮次
    def _start_round(self):
        # 先取得会话再改动本地状态；open_session 失败时不留任何半成品轮次。
        revision = self.client.open_session()
        round_no = len(self.state["rounds"]) + 1
        rnd = {
            "round_no": round_no,
            "revision": revision,
            "status": "active",
            "budget": self.budget,
            "queries": 0,
            "events": [],
            "tiles": [
                {"index": i, "root": root.to_dict(), "queue": [], "terminal": []}
                for i, root in enumerate(make_tiles(self.scope, self.tiles))
            ],
        }
        for tile in rnd["tiles"]:
            tile["queue"].append([Cell.from_dict(tile["root"]).to_dict(), []])
        self.state["rounds"].append(rnd)
        # 立即落盘，保证轮次编号、revision 与队列起点在崩溃后仍一致。
        self._save()
        return rnd

    def _active_round(self):
        for rnd in reversed(self.state["rounds"]):
            if rnd["status"] == "active":
                return rnd
        return None

    # ------------------------------------------------------------------ 去重
    def _merge_records(self, records, rnd, cell, cell_status):
        catalog = self.state["catalog"]
        provenance = {
            "round_no": rnd["round_no"],
            "revision": rnd["revision"],
            "cell": cell.to_dict(),
            "cell_status": cell_status,
        }
        for record in records:
            sid = record["source_id"]
            tagged = dict(record)
            tagged["_harvest"] = dict(provenance)
            old = catalog.get(sid)
            if old is None:
                catalog[sid] = tagged
                continue
            old_round = old["_harvest"]["round_no"]
            if old_round > rnd["round_no"]:
                # 绝不用旧轮次数据覆盖更新轮次已经拿到的记录。
                continue
            if old_round == rnd["round_no"]:
                # 同一 revision 快照内重复见到（防御性：正常二分划分互不重叠）。
                # 这是边界重复返回，不是重复资料：保留原记录，只追加溯源。
                old["_harvest"].setdefault("also_seen_in", []).append(
                    {"cell": cell.to_dict(), "cell_status": cell_status}
                )
                continue
            catalog[sid] = tagged  # 较新 revision 覆盖较旧记录

    # ------------------------------------------------------------------ 主循环
    def run(self, max_new_rounds=3):
        """推进采集，返回最新状态。

        每次调用向当前活跃轮次追加 ``budget`` 个请求额度；若没有活跃轮次
        （首次运行、上轮已完成或上轮因 revision 过期被冻结），则开启新会话、
        启动全新轮次并赋予完整 budget。一次调用内因过期最多开启
        ``max_new_rounds`` 个新会话。
        """
        new_rounds = 0
        while True:
            rnd = self._active_round()
            if rnd is None:
                if new_rounds >= max_new_rounds:
                    break
                rnd = self._start_round()
                new_rounds += 1
            else:
                # 同一会话续采：本次预算追加到该轮，支持扩大预算后续采。
                rnd["budget"] += self.budget
                self._save()
            try:
                self._drive_round(rnd)
            except SessionExpired as exc:
                rnd["status"] = "abandoned_expired"
                rnd["events"].append({
                    "type": "expired",
                    "revision": exc.revision,
                    "message": str(exc),
                })
                self._save()
                # 本轮不宣告完整；新会话从新的轮次开始，不拼接旧 revision 片。
                continue
            break
        return self.state

    def _drive_round(self, rnd):
        tiles = rnd["tiles"]
        while rnd["queries"] < rnd["budget"] and rnd["status"] == "active":
            progressed = False
            # 大区轮询：每趟每个未完成大区至多发一个请求，保证高密度区
            # 无法独占预算，所有大区被公平推进。
            for tile in tiles:
                if rnd["queries"] >= rnd["budget"]:
                    break
                if not tile["queue"]:
                    continue
                progressed = True
                cell_dict, chain = tile["queue"].pop(0)
                cell = Cell.from_dict(cell_dict)
                payload = self.client.query(rnd["revision"], cell)
                rnd["queries"] += 1
                request_log = {
                    "cell": cell.to_dict(),
                    "count": payload["count"],
                    "truncated": bool(payload["truncated"]),
                    "returned": len(payload["records"]),
                    "revision": payload["revision"],
                }
                chain.append(request_log)

                if not payload["truncated"]:
                    cell_status = "complete"
                    self._merge_records(payload["records"], rnd, cell, cell_status)
                    tile["terminal"].append({
                        "tile_index": tile["index"],
                        "cell": cell.to_dict(),
                        "status": cell_status,
                        "count": payload["count"],
                        "requests": chain,
                        "record_ids": [r["source_id"] for r in payload["records"]],
                    })
                else:
                    children = split_cell(cell)
                    if not children:
                        # 空间只剩一个整数点且时间宽度为一，同坐标同时刻
                        # 记录数仍超过上限：不可解单元，停止细分。
                        # 上限内可见的记录仍是有效资料，并入 catalog 并
                        # 标注来自不可解单元；缺口由报告显式说明，不冒充完整。
                        cell_status = "unsolvable"
                        self._merge_records(
                            payload["records"], rnd, cell, cell_status)
                        tile["terminal"].append({
                            "tile_index": tile["index"],
                            "cell": cell.to_dict(),
                            "status": cell_status,
                            "count": payload["count"],
                            "requests": chain,
                            "record_ids": [r["source_id"] for r in payload["records"]],
                            "missing_at_least": payload["count"] - len(payload["records"]),
                        })
                    else:
                        # 子单元带上父请求链，使每个最终单元都能回溯全部来源请求。
                        inserted = [
                            [child.to_dict(), list(chain)] for child in reversed(children)
                        ]
                        tile["queue"][0:0] = inserted
                self._save()
            if not progressed:
                break
        if rnd["status"] == "active" and all(not t["queue"] for t in tiles):
            rnd["status"] = "completed"
            self._save()
