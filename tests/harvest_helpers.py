"""采集器测试辅助：内存假来源与随机数据生成。

假来源严格复刻 sources/contract.json 的协议：count 是截断前数量、
truncated 表示超过上限、revision 按 generation 编号、generation 变化后
旧 revision 以 410 失效。数据全部在内存中，随机检查不触碰仓库样本文件。
"""
import random

from harvester.cells import Cell
from harvester.client import SessionExpired, SourceError


class FakeSourceClient:
    """与 :class:`harvester.client.DirectClient` 同接口的内存来源。"""

    def __init__(self, records, limit=3, seed=0):
        self.records = list(records)
        self.limit = limit
        self.generation = 1
        self.phase = "base"
        self.query_log = []
        self.queries_until_bump = None  # 设置为整数：再发 N 个查询后 generation+1
        self._queries_since_arm = 0
        self.seed = seed

    def open_session(self):
        if self.phase != "base":
            raise SourceError("来源处于 expired 阶段，无法开启会话")
        return "r" + str(self.generation)

    def query(self, revision, cell):
        if self.queries_until_bump is not None:
            self._queries_since_arm += 1
            if self._queries_since_arm >= self.queries_until_bump:
                self.generation += 1
                self.queries_until_bump = None
        if self.phase == "expired" or revision != "r" + str(self.generation):
            raise SessionExpired(revision)
        if not isinstance(cell, Cell):
            raise TypeError("cell 必须是 Cell")
        matched = [
            r for r in self.records
            if cell.x0 <= r["x"] <= cell.x1
            and cell.y0 <= r["y"] <= cell.y1
            and cell.t0 <= r["updated_at"] < cell.t1
        ]
        matched.sort(key=lambda r: r["source_id"])
        self.query_log.append({
            "revision": revision,
            "cell": cell.to_dict(),
            "count": len(matched),
        })
        return {
            "count": len(matched),
            "truncated": len(matched) > self.limit,
            "revision": revision,
            "records": [dict(r) for r in matched[:self.limit]],
        }


def ground_truth(records, cell):
    """直接从数据集枚举 cell 内的真实记录（检查用 oracle，不经过接口）。"""
    return [
        r for r in records
        if cell.x0 <= r["x"] <= cell.x1
        and cell.y0 <= r["y"] <= cell.y1
        and cell.t0 <= r["updated_at"] < cell.t1
    ]


def make_random_records(rng, n, x_span, y_span, t_span, hotspot=None):
    """生成随机合成点位；hotspot=(x,y,t,count) 制造同刻多记录的极密点。"""
    records = []
    for i in range(n):
        records.append({
            "source_id": f"r{i:05d}",
            "x": rng.randrange(x_span),
            "y": rng.randrange(y_span),
            "updated_at": rng.randrange(t_span),
            "name": f"随机点{i}",
        })
    if hotspot is not None:
        hx, hy, ht, count = hotspot
        start = len(records)
        for j in range(count):
            records.append({
                "source_id": f"h{start + j:05d}",
                "x": hx,
                "y": hy,
                "updated_at": ht,
                "name": f"极密点{j}",
            })
    return records


def fresh_rng(seed):
    return random.Random(seed)
