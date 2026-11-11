"""采集器自动检查：穷举对照、不可解单元、会话切换、预算与续采、公平性。"""
import os
import tempfile
import unittest
from urllib.request import urlopen

from harvester import HarvestEngine, build_report
from harvester.cells import Cell, make_tiles, split_cell
from harvester.client import DirectClient, HttpClient, SessionExpired
from harvester.report import write_outputs
from source_site.source import Source
from source_site.__main__ import running

try:  # 同时兼容 python -m unittest discover 与 python3 -I run_checks.py
    from .harvest_helpers import (
        FakeSourceClient,
        fresh_rng,
        ground_truth,
        make_random_records,
    )
except ImportError:  # pragma: no cover - 仅加载方式不同
    from harvest_helpers import (
        FakeSourceClient,
        fresh_rng,
        ground_truth,
        make_random_records,
    )


def _run_engine(records, scope, budget, tiles=4, limit=3, state_path=None,
                client=None, max_new_rounds=3):
    client = client or FakeSourceClient(records, limit=limit)
    state_path = state_path or os.path.join(tempfile.mkdtemp(), "state.json")
    engine = HarvestEngine(client, state_path, scope, budget=budget,
                           tiles=tiles, limit=limit)
    state = engine.run(max_new_rounds=max_new_rounds)
    return client, state, state_path


def _all_terminal_cells(rnd):
    return [entry for tile in rnd["tiles"] for entry in tile["terminal"]]


def _all_queue_cells(rnd):
    return [Cell.from_dict(cell) for tile in rnd["tiles"]
            for cell, _chain in tile["queue"]]


class CellSplitTests(unittest.TestCase):
    def test_atomic_cell_cannot_split(self):
        cell = Cell(5, 7, 2, 5, 7, 3)  # 单点、时间宽度一
        self.assertTrue(cell.is_atomic())
        self.assertEqual([], split_cell(cell))

    def test_split_is_exhaustive_and_disjoint(self):
        rng = fresh_rng(101)
        for _ in range(200):
            cell = Cell(
                rng.randrange(0, 4), rng.randrange(0, 4), rng.randrange(0, 3),
                rng.randrange(4, 9), rng.randrange(4, 9), rng.randrange(4, 8),
            )
            children = split_cell(cell)
            self.assertEqual(2, len(children))
            # 用一批随机点验证：每个点恰属于一个子单元，且归属集合等于父单元。
            points = [
                (rng.randrange(cell.x0, cell.x1 + 1),
                 rng.randrange(cell.y0, cell.y1 + 1),
                 rng.randrange(cell.t0, cell.t1))
                for _ in range(50)
            ]

            def belongs(pt, c):
                x, y, t = pt
                return (c.x0 <= x <= c.x1 and c.y0 <= y <= c.y1
                        and c.t0 <= t < c.t1)

            for pt in points:
                self.assertTrue(belongs(pt, cell))
                owners = [c for c in children if belongs(pt, c)]
                self.assertEqual(1, len(owners), f"边界点重复归属: {pt}")

    def test_time_split_is_half_open(self):
        cell = Cell(0, 0, 0, 0, 0, 2)
        left, right = split_cell(cell)
        self.assertEqual((0, 1), (left.t0, left.t1))
        self.assertEqual((1, 2), (right.t0, right.t1))

    def test_make_tiles_partition_scope(self):
        root = Cell(0, 0, 0, 9, 9, 5)
        for k in (1, 2, 3, 4, 8, 16, 100):
            tiles = make_tiles(root, k)
            seen = set()
            for tile in tiles:
                for x in range(tile.x0, tile.x1 + 1):
                    for y in range(tile.y0, tile.y1 + 1):
                        seen.add((x, y))
            expected = {(x, y) for x in range(10) for y in range(10)}
            self.assertEqual(expected, seen)
            for tile in tiles:
                self.assertEqual((0, 5), (tile.t0, tile.t1))


class ExhaustiveOracleTests(unittest.TestCase):
    """随机小范围 + 穷举结果对照（多组种子）。"""

    def _assert_partition_invariant(self, state, round_no=None):
        rnd = state["rounds"][round_no - 1] if round_no else state["rounds"][-1]
        scope = Cell.from_dict(state["scope"])
        cells = ([Cell.from_dict(e["cell"]) for e in _all_terminal_cells(rnd)]
                 + _all_queue_cells(rnd))
        seen = set()
        for cell in cells:
            for x in range(cell.x0, cell.x1 + 1):
                for y in range(cell.y0, cell.y1 + 1):
                    for t in range(cell.t0, cell.t1):
                        key = (x, y, t)
                        self.assertNotIn(key, seen, f"划分重叠: {key}")
                        seen.add(key)
        expected = {
            (x, y, t)
            for x in range(scope.x0, scope.x1 + 1)
            for y in range(scope.y0, scope.y1 + 1)
            for t in range(scope.t0, scope.t1)
        }
        self.assertEqual(expected, seen)

    def test_random_ranges_no_missing_points(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                rng = fresh_rng(1000 + seed)
                span = rng.randrange(3, 7)
                t_span = rng.randrange(2, 5)
                n = rng.randrange(0, 40)
                records = make_random_records(rng, n, span, span, t_span)
                scope = Cell(0, 0, 0, span - 1, span - 1, t_span)
                client, state, _ = _run_engine(records, scope, budget=10000)
                report = build_report(state)

                self.assertTrue(report["fully_covered"], report["statement"])
                self.assertEqual([], report["unsolvable_cells"])
                self.assertEqual([], report["pending_units"])
                self._assert_partition_invariant(state)

                # 穷举 oracle：catalog 与逐点枚举完全一致，不漏不重。
                truth = ground_truth(records, scope)
                self.assertEqual(
                    sorted(r["source_id"] for r in truth),
                    sorted(state["catalog"].keys()),
                )
                rnd = state["rounds"][-1]
                for entry in _all_terminal_cells(rnd):
                    cell = Cell.from_dict(entry["cell"])
                    self.assertEqual("complete", entry["status"])
                    self.assertFalse(entry["requests"][-1]["truncated"])
                    expected_ids = sorted(
                        r["source_id"] for r in ground_truth(records, cell))
                    self.assertEqual(expected_ids, sorted(entry["record_ids"]))

    def test_atomic_cell_within_limit_is_complete(self):
        # 原子单元（单点、时间宽度一）但记录数不超过上限：必须报完整，
        # 不能因为不可再分就一律标记为不可解。
        records = [
            {"source_id": f"a{i}", "x": 4, "y": 4, "updated_at": 1, "name": str(i)}
            for i in range(3)
        ]
        scope = Cell(4, 4, 1, 4, 4, 2)
        client, state, _ = _run_engine(records, scope, budget=10, tiles=1)
        report = build_report(state)
        self.assertTrue(report["fully_covered"])
        self.assertEqual(1, len(report["complete_cells"]))
        self.assertEqual([], report["unsolvable_cells"])

    def test_budget_smaller_than_tile_count(self):
        # 预算连每个大区一个根请求都不够：只有部分大区被触达，
        # 未触达大区的根单元必须原样保留为 pending，重启可继续。
        rng = fresh_rng(1500)
        records = make_random_records(rng, 30, 12, 12, 3)
        scope = Cell(0, 0, 0, 11, 11, 3)
        client, state, path = _run_engine(
            records, scope, budget=2, tiles=4)
        report = build_report(state)
        self.assertEqual(2, state["rounds"][-1]["queries"])
        self.assertTrue(report["pending_units"])
        rnd = state["rounds"][-1]
        touched = set()
        for tile in rnd["tiles"]:
            for entry in tile["terminal"]:
                for req in entry["requests"]:
                    touched.add(tile["index"])
            for _cell, chain in tile["queue"]:
                for _req in chain:
                    touched.add(tile["index"])
        self.assertEqual({0, 1}, touched)  # 轮询从低 index 大区开始
        # 未触达大区根单元仍是完整范围，未被修改。
        untouched = rnd["tiles"][3]
        self.assertEqual(1, len(untouched["queue"]))
        self.assertEqual(
            Cell.from_dict(untouched["root"]),
            Cell.from_dict(untouched["queue"][0][0]),
        )
        # 续采后整体采全。
        engine2 = HarvestEngine(client, path, scope, budget=10000, tiles=4)
        state2 = engine2.run()
        self.assertTrue(build_report(state2)["fully_covered"])
        self.assertEqual(1, len(state2["rounds"]))

    def test_hotspot_cells_are_unsolvable_never_complete(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                rng = fresh_rng(2000 + seed)
                span, t_span = 6, 4
                records = make_random_records(
                    rng, 15, span, span, t_span,
                    hotspot=(2, 3, 1, rng.randrange(4, 9)),  # 同刻 4~8 条 > 上限3
                )
                scope = Cell(0, 0, 0, span - 1, span - 1, t_span - 1)
                client, state, _ = _run_engine(records, scope, budget=10000)
                report = build_report(state)

                self.assertFalse(report["fully_covered"])
                unsolvable = report["unsolvable_cells"]
                self.assertTrue(unsolvable)
                for entry in unsolvable:
                    cell = Cell.from_dict(entry["cell"])
                    # 不可解单元必须已经细分到原子尺度。
                    self.assertTrue(cell.is_atomic())
                    self.assertEqual((2, 3, 1, 2, 3, 2), cell.key())
                    self.assertGreaterEqual(entry["missing_at_least"], 1)
                    # 关键约束：不可解单元没有被报为完整。
                    self.assertEqual("unsolvable", entry["status"])
                    self.assertNotIn(
                        entry["cell"],
                        [e["cell"] for e in report["complete_cells"]],
                    )

                # 正常区域仍然没有漏点：除极密原子点外，穷举一致。
                truth_normal = [
                    r for r in ground_truth(records, scope)
                    if (r["x"], r["y"], r["updated_at"]) != (2, 3, 1)
                ]
                complete_ids = {
                    sid for e in report["complete_cells"]
                    for sid in e["record_ids"]
                }
                self.assertEqual(
                    sorted(r["source_id"] for r in truth_normal),
                    sorted(complete_ids),
                )
                # 不可解单元中至少取回上限内的记录，且明确标注缺口，不冒充完整。
                hot = unsolvable[0]
                self.assertEqual(3, len(hot["record_ids"]))
                self.assertEqual(
                    hot["count"] - 3, hot["missing_at_least"])
                self._assert_partition_invariant(state)


class BudgetAndResumeTests(unittest.TestCase):
    def test_small_budget_resumes_without_restarting_session(self):
        rng = fresh_rng(3000)
        records = make_random_records(rng, 60, 8, 8, 4)
        scope = Cell(0, 0, 0, 7, 7, 4)
        tmp = tempfile.mkdtemp()
        state_path = os.path.join(tmp, "state.json")
        client = FakeSourceClient(records)

        # 一次性跑全的参照结果。
        ref_client, ref_state, _ = _run_engine(
            records, scope, budget=10000, tiles=4)
        ref_queries = ref_client.query_log

        # 极小预算多次续采，每次都从 pending 单元继续，不重开轮次。
        chunks = [2, 3, 1, 4, 2, 5, 3, 10000]
        for i, budget in enumerate(chunks):
            engine = HarvestEngine(client, state_path, scope,
                                   budget=budget, tiles=4)
            state = engine.run()
            if i < len(chunks) - 1:
                self.assertEqual(
                    "active", state["rounds"][-1]["status"],
                    "预算未耗尽采集时轮次应保持 active 可续采",
                )
        report = build_report(state)
        self.assertTrue(report["fully_covered"])
        self.assertEqual(1, len(state["rounds"]), "同一会话续采不得新开轮次")
        self.assertEqual(state["rounds"][0]["revision"], "r1")
        # 续采与一次性采集对同一来源发出的有效查询数一致（没有重复请求冒充）。
        self.assertEqual(len(ref_queries), client.query_log.__len__())
        self.assertEqual(
            sorted(ref_state["catalog"].keys()), sorted(state["catalog"].keys()))

    def test_pending_units_are_directly_resumable(self):
        rng = fresh_rng(3001)
        records = make_random_records(rng, 50, 8, 8, 4)
        scope = Cell(0, 0, 0, 7, 7, 4)
        client, state, path = _run_engine(records, scope, budget=3, tiles=4)
        report = build_report(state)
        self.assertTrue(report["pending_units"])
        pending_cells = [u["cell"] for u in report["pending_units"]]
        rnd = state["rounds"][-1]
        queued = [c for tile in rnd["tiles"] for c, _ in tile["queue"]]
        self.assertEqual(pending_cells, queued)
        # 重启进程（新引擎实例、同一状态文件）继续。
        engine2 = HarvestEngine(client, path, scope, budget=10000, tiles=4)
        state2 = engine2.run()
        self.assertTrue(build_report(state2)["fully_covered"])

    def test_scope_mismatch_is_rejected(self):
        rng = fresh_rng(3002)
        records = make_random_records(rng, 10, 6, 6, 3)
        client, _state, path = _run_engine(
            records, Cell(0, 0, 0, 5, 5, 3), budget=5)
        with self.assertRaises(ValueError):
            HarvestEngine(client, path, Cell(0, 0, 0, 4, 5, 3), budget=5)


class FairnessTests(unittest.TestCase):
    def test_round_robin_advances_every_tile_before_drain(self):
        # 一个极密热点放在 0 号 tile，其余区域稀疏。
        rng = fresh_rng(4000)
        records = make_random_records(rng, 10, 16, 4, 4,
                                      hotspot=(1, 1, 1, 30))
        scope = Cell(0, 0, 0, 15, 3, 4)
        client = FakeSourceClient(records)
        path = os.path.join(tempfile.mkdtemp(), "state.json")
        engine = HarvestEngine(client, path, scope, budget=8, tiles=4)
        state = engine.run()
        rnd = state["rounds"][-1]
        per_tile_queries = {}
        for tile in rnd["tiles"]:
            request_cells = set()
            for entry in tile["terminal"]:
                for req in entry["requests"]:
                    request_cells.add(Cell.from_dict(req["cell"]).key())
            for _cell, chain in tile["queue"]:
                for req in chain:
                    request_cells.add(Cell.from_dict(req["cell"]).key())
            per_tile_queries[tile["index"]] = len(request_cells)
        self.assertEqual(4, len(per_tile_queries))
        # 公平性：四个大区都被触达，高密度 tile 没有独占预算；
        # 8 个请求全部落在大区轮询序列上。
        self.assertTrue(all(v >= 1 for v in per_tile_queries.values()))
        self.assertEqual(8, sum(per_tile_queries.values()))
        # 按请求顺序重建调度序列，验证轮询性质：每一趟中同一 tile
        # 至多出现一次，且各趟按 tile index 升序访问仍有队列的大区。
        tile_of_cell = {}
        for tile in rnd["tiles"]:
            for entry in tile["terminal"]:
                for req in entry["requests"]:
                    tile_of_cell[Cell.from_dict(req["cell"]).key()] = tile["index"]
            for cell, chain in tile["queue"]:
                for req in chain:
                    tile_of_cell[Cell.from_dict(req["cell"]).key()] = tile["index"]
        order = [
            tile_of_cell[Cell.from_dict(q["cell"]).key()]
            for q in client.query_log
        ]
        self.assertEqual([0, 1, 2, 3], order[:4])  # 第一趟严格轮询四个大区
        for i in range(1, len(order)):
            self.assertNotEqual(order[i], order[i - 1])  # 不会连续轰炸同一大区
        self.assertTrue(build_report(state)["pending_units"])


class RevisionSwitchTests(unittest.TestCase):
    """会话切换检查：证实旧 revision 记录不会混入新轮次的完整结论。"""

    def test_expiry_freezes_round_and_new_round_restarts_clean(self):
        rng = fresh_rng(5000)
        records = make_random_records(rng, 40, 8, 8, 4)
        scope = Cell(0, 0, 0, 7, 7, 4)
        client = FakeSourceClient(records)
        path = os.path.join(tempfile.mkdtemp(), "state.json")

        engine = HarvestEngine(client, path, scope, budget=5, tiles=4)
        # 第 3 个查询后来源 generation+1，旧 revision 立即失效。
        client.queries_until_bump = 3
        state = engine.run()
        r1 = state["rounds"][0]
        self.assertEqual("abandoned_expired", r1["status"])
        self.assertEqual("r1", r1["revision"])
        self.assertTrue(r1["events"])
        # 过期轮不提供任何覆盖结论。
        report = build_report(state)
        self.assertNotEqual(1, report["coverage_round"])
        self.assertFalse(report["fully_covered"])

        # 新会话从新轮次开始：revision 变为 r2，且空间片从头重采。
        engine2 = HarvestEngine(client, path, scope, budget=10000, tiles=4)
        state2 = engine2.run()
        self.assertEqual(2, len(state2["rounds"]))
        r2 = state2["rounds"][1]
        self.assertEqual("r2", r2["revision"])
        self.assertEqual("completed", r2["status"])
        report2 = build_report(state2)
        self.assertEqual(2, report2["coverage_round"])
        self.assertTrue(report2["fully_covered"])

        # 完整结论只引用 r2：所有 complete 单元的请求 revision 全是 r2。
        for entry in report2["complete_cells"]:
            for req in entry["requests"]:
                self.assertEqual("r2", req["revision"])

        # catalog 中当前轮记录全部带 r2 溯源；不允许 r1 冒充当前资料。
        for sid, rec in state2["catalog"].items():
            self.assertEqual(2, rec["_harvest"]["round_no"])
            self.assertEqual("r2", rec["_harvest"]["revision"])

    def test_expired_session_on_restart_does_not_mix_old_revision(self):
        rng = fresh_rng(5001)
        records = make_random_records(rng, 30, 8, 8, 4)
        scope = Cell(0, 0, 0, 7, 7, 4)
        client = FakeSourceClient(records)
        path = os.path.join(tempfile.mkdtemp(), "state.json")

        engine = HarvestEngine(client, path, scope, budget=4, tiles=4)
        state = engine.run()
        self.assertEqual("active", state["rounds"][-1]["status"])
        # 模拟进程重启期间来源 generation 变化：状态里仍是 r1。
        client.generation += 1

        engine2 = HarvestEngine(client, path, scope, budget=10000, tiles=4)
        state2 = engine2.run()
        self.assertIn("abandoned_expired",
                      [r["status"] for r in state2["rounds"]])
        last = state2["rounds"][-1]
        self.assertEqual("r2", last["revision"])
        report = build_report(state2)
        self.assertEqual(last["round_no"], report["coverage_round"])
        for entry in report["complete_cells"]:
            for req in entry["requests"]:
                self.assertEqual("r2", req["revision"])

    def test_direct_client_raises_on_410(self):
        source = Source()
        source.phase = "expired"
        client = DirectClient(source)
        with self.assertRaises(SessionExpired):
            client.query("r1", Cell(0, 0, 0, 2, 2, 2))


class RepositorySampleTests(unittest.TestCase):
    """用仓库自带合成 points.json 端到端跑通，含已知不可解单元。"""

    def test_sample_data_end_to_end(self):
        source = Source()
        client = DirectClient(source)
        path = os.path.join(tempfile.mkdtemp(), "state.json")
        engine = HarvestEngine(
            client, path, Cell(0, 0, 0, 10, 10, 5),
            budget=100, tiles=4)
        state = engine.run()
        report = build_report(state)
        self.assertFalse(report["fully_covered"])
        unsolvable = report["unsolvable_cells"]
        self.assertEqual(1, len(unsolvable))
        cell = Cell.from_dict(unsolvable[0]["cell"])
        self.assertEqual((1, 1, 2, 1, 1, 3), cell.key())
        self.assertEqual(5, unsolvable[0]["count"])
        self.assertEqual(2, unsolvable[0]["missing_at_least"])
        # 其余 4 条正常记录全部确证采全。
        self.assertEqual(
            {"p5", "p6", "p7", "p8"},
            {sid for e in report["complete_cells"] for sid in e["record_ids"]},
        )
        # catalog 含 7 个可取回的来源编号：不可解单元上限内可见 p0~p2，
        # p3/p4 超过来源单请求上限且单元已不可再分，任何子查询都无法取得。
        self.assertEqual(7, len(state["catalog"]))
        self.assertEqual(
            {"p0", "p1", "p2", "p5", "p6", "p7", "p8"},
            set(state["catalog"].keys()),
        )
        # 报告必须显式保留这一无法消除的截断，而不是假装 9 条已收齐。
        missing_ids = set()
        for entry in report["unsolvable_cells"]:
            missing_ids.update(
                f"p{i}" for i in range(9)
                if f"p{i}" not in state["catalog"]
            )
        self.assertEqual({"p3", "p4"}, missing_ids)

    def test_http_end_to_end(self):
        with running() as (base, source):
            # 先确认服务可用。
            with urlopen(base + "/session", timeout=3) as response:
                self.assertEqual(200, response.status)
            client = HttpClient(base)
            path = os.path.join(tempfile.mkdtemp(), "state.json")
            engine = HarvestEngine(
                client, path, Cell(0, 0, 0, 10, 10, 5),
                budget=100, tiles=4)
            state = engine.run()
            report = build_report(state)
            self.assertEqual(1, len(report["unsolvable_cells"]))
            out_dir = tempfile.mkdtemp()
            report2 = write_outputs(state, out_dir)
            self.assertTrue(
                os.path.exists(os.path.join(out_dir, "records.json")))
            self.assertEqual(
                len(report2["unsolvable_cells"]), 1)


class DeduplicationTests(unittest.TestCase):
    def test_source_id_dedup_and_new_revision_overwrites(self):
        # 两个 revision 的数据集：同 source_id 业务内容更新，另有新增/删除。
        v1 = [
            {"source_id": "a", "x": 0, "y": 0, "updated_at": 0, "name": "旧"},
            {"source_id": "b", "x": 1, "y": 0, "updated_at": 0, "name": "b1"},
        ]
        v2 = [
            {"source_id": "a", "x": 0, "y": 0, "updated_at": 0, "name": "新"},
            {"source_id": "c", "x": 1, "y": 0, "updated_at": 0, "name": "c1"},
        ]
        client = FakeSourceClient(v1)
        path = os.path.join(tempfile.mkdtemp(), "state.json")
        scope = Cell(0, 0, 0, 1, 0, 1)
        engine = HarvestEngine(client, path, scope, budget=50, tiles=1)
        state = engine.run()
        self.assertEqual("旧", state["catalog"]["a"]["name"])

        # 来源更新数据并使旧 revision 失效，新轮次重采覆盖。
        client.records = v2
        client.generation += 1
        engine2 = HarvestEngine(client, path, scope, budget=50, tiles=1)
        state2 = engine2.run()
        self.assertEqual("新", state2["catalog"]["a"]["name"])
        self.assertNotIn("b", state2["catalog"].get("a", {}).get("name", ""))
        self.assertEqual(2, state2["catalog"]["a"]["_harvest"]["round_no"])
        self.assertIn("c", state2["catalog"])
        report = build_report(state2)
        current_ids = {
            sid for e in report["complete_cells"]
            for sid in e["record_ids"]
        }
        self.assertEqual({"a", "c"}, current_ids)  # 旧轮 b 不混入新结论


if __name__ == "__main__":
    unittest.main()
