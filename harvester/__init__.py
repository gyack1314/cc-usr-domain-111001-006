"""受结果上限约束的空间分片采集器。

仅依赖标准库。核心入口：

- :class:`harvester.cells.Cell`：整数坐标闭区间 bbox + 左闭右开时间窗
- :class:`harvester.engine.HarvestEngine`：分片采集、预算、revision 会话与断点续采
- :func:`harvester.report.build_report`：完整性结论与不可消除截断说明
"""
from .cells import Cell, make_tiles, split_cell
from .client import DirectClient, HttpClient, SessionExpired, SourceError
from .engine import HarvestEngine
from .report import build_report

__all__ = [
    "Cell",
    "make_tiles",
    "split_cell",
    "DirectClient",
    "HttpClient",
    "SessionExpired",
    "SourceError",
    "HarvestEngine",
    "build_report",
]
