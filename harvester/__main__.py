"""命令行入口。

示例（进程内直连仓库自带的合成来源）：

    python -m harvester --bbox 0,0,10,10 --time 0,5 --budget 20 \\
        --tiles 4 --out state/run1

示例（对接本地 HTTP 来源）：

    python -m harvester --http-url http://127.0.0.1:8090 \\
        --bbox 0,0,10,10 --time 0,5 --budget 20 --out state/run1

扩大预算后续采而不是从头开始：对同一 --out 目录再次运行即可，状态文件中
同一会话的活跃轮次会追加预算并从 pending 单元继续。
"""
import argparse
import json
import sys

from .cells import Cell
from .client import DirectClient, HttpClient
from .engine import HarvestEngine
from .report import build_report, write_outputs

EXIT_OK = 0
EXIT_PENDING = 1
EXIT_UNSOLVABLE = 2
EXIT_SOURCE_FAILURE = 3


def _parse_int_list(text, length, name):
    try:
        values = [int(part) for part in text.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} 必须是逗号分隔的整数") from exc
    if len(values) != length:
        raise argparse.ArgumentTypeError(f"{name} 需要 {length} 个整数")
    return values


def build_parser():
    parser = argparse.ArgumentParser(description="受结果上限约束的空间分片采集器")
    parser.add_argument("--bbox", required=True,
                        help="整数闭区间 bbox：x0,y0,x1,y1")
    parser.add_argument("--time", required=True,
                        help="左闭右开整数逻辑时间窗：t0,t1")
    parser.add_argument("--budget", required=True, type=int,
                        help="本次运行的请求预算（按 /points 请求计数）")
    parser.add_argument("--tiles", type=int, default=4,
                        help="顶层大区分片数（公平调度单位，默认 4）")
    parser.add_argument("--limit", type=int, default=3,
                        help="来源单请求记录上限（默认 3，与合成来源一致）")
    parser.add_argument("--out", default="state/harvest",
                        help="状态与报告输出目录（默认 state/harvest）")
    parser.add_argument("--max-new-rounds", type=int, default=3,
                        help="一次运行内因 revision 过期最多开启的新轮次数")
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--http-url",
                            help="本地 HTTP 来源地址，如 http://127.0.0.1:8090")
    connection.add_argument("--source-data",
                            help="进程内直连来源所读 points.json 路径（"
                                 "默认使用仓库 sources/points.json）")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    x0, y0, x1, y1 = _parse_int_list(args.bbox, 4, "--bbox")
    t0, t1 = _parse_int_list(args.time, 2, "--time")
    scope = Cell(x0, y0, t0, x1, y1, t1)

    if args.http_url:
        client = HttpClient(args.http_url)
    else:
        from pathlib import Path

        from source_site import source as source_module
        from source_site.source import Source

        if args.source_data:
            # Source 通过模块级 DATA 目录读取 points.json，指向自定义数据目录。
            source_module.DATA = Path(args.source_data).resolve().parent
        client = DirectClient(Source())

    state_path = args.out.rstrip("/") + "/state.json"
    engine = HarvestEngine(
        client, state_path, scope,
        budget=args.budget, tiles=args.tiles, limit=args.limit,
    )
    try:
        state = engine.run(max_new_rounds=args.max_new_rounds)
    except Exception as exc:  # 协议级失败：不落任何完整性结论
        print(f"来源访问失败，已保留已有状态，未宣告完整：{exc}", file=sys.stderr)
        return EXIT_SOURCE_FAILURE

    report = write_outputs(state, args.out)
    summary = build_report(state)
    print(json.dumps({
        "coverage_round": summary["coverage_round"],
        "coverage_revision": summary.get("coverage_revision"),
        "fully_covered": summary["fully_covered"],
        "complete_cells": len(summary["complete_cells"]),
        "unsolvable_cells": len(summary["unsolvable_cells"]),
        "pending_units": len(summary["pending_units"]),
        "rounds": [(r["round_no"], r["revision"], r["status"], r["queries_used"])
                   for r in summary["rounds"]],
    }, ensure_ascii=False, indent=2))
    print(summary["statement"])
    print(f"输出目录：{args.out}（state.json / report.json / records.json）")

    if summary["unsolvable_cells"]:
        return EXIT_UNSOLVABLE
    if summary["pending_units"]:
        return EXIT_PENDING
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
