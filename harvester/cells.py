"""空间/时间单元与确定性二分拆分。

单元语义与来源接口一致：bbox 是整数坐标闭区间，时间是整数逻辑时间左闭右开。
所有拆分都产生互不重叠（按 (x, y, updated_at) 归属）且并集恰等于父单元的
子单元，因此只要每个子单元被完整取回，就不存在漏点。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Cell:
    """一个可查询单元：x0<=x<=x1、y0<=y<=y1、t0<=updated_at<t1。"""

    x0: int
    y0: int
    t0: int
    x1: int
    y1: int
    t1: int

    def __post_init__(self):
        if not (
            isinstance(self.x0, int)
            and isinstance(self.y0, int)
            and isinstance(self.t0, int)
            and isinstance(self.x1, int)
            and isinstance(self.y1, int)
            and isinstance(self.t1, int)
        ):
            raise TypeError("单元边界必须是整数")
        if self.x0 > self.x1 or self.y0 > self.y1:
            raise ValueError("空间区间不能为空")
        if self.t0 >= self.t1:
            raise ValueError("时间区间必须为左闭右开且非空")

    @property
    def x_width(self) -> int:
        """x 方向整数点数（闭区间）。"""
        return self.x1 - self.x0 + 1

    @property
    def y_width(self) -> int:
        return self.y1 - self.y0 + 1

    @property
    def t_width(self) -> int:
        """时间窗内整数时刻数（左闭右开）。"""
        return self.t1 - self.t0

    def is_atomic(self) -> bool:
        """只剩一个整数空间点且时间宽度为一，任何维度都无法再拆。"""
        return self.x_width == 1 and self.y_width == 1 and self.t_width == 1

    def bbox_param(self) -> str:
        return f"{self.x0},{self.y0},{self.x1},{self.y1}"

    def time_param(self) -> str:
        return f"{self.t0},{self.t1}"

    def to_dict(self) -> dict:
        return {
            "x0": self.x0,
            "y0": self.y0,
            "t0": self.t0,
            "x1": self.x1,
            "y1": self.y1,
            "t1": self.t1,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Cell":
        return cls(
            data["x0"], data["y0"], data["t0"],
            data["x1"], data["y1"], data["t1"],
        )

    def key(self) -> tuple:
        return (self.x0, self.y0, self.t0, self.x1, self.y1, self.t1)


def _split_point(lo: int, hi: int) -> int:
    """闭区间 [lo, hi] 的切分点，左段为 [lo, mid]。"""
    return lo + (hi - lo) // 2


def split_cell(cell: Cell) -> list[Cell]:
    """把单元按最宽维度二分；同宽时优先 x，再 y，最后 t。

    - 空间按闭区间切：[lo, mid] 与 [mid+1, hi]，边界点只属于一侧，
      不会因为闭区间重合而在两个子单元重复出现。
    - 时间按左闭右开切：[t0, mid) 与 [mid, t1)，语义与来源一致。
    - 原子单元（空间单点且时间宽度一）返回空列表，由调用方记为不可解。
    """
    widths = (("x", cell.x_width), ("y", cell.y_width), ("t", cell.t_width))
    axis, width = max(widths, key=lambda item: item[1])
    if width <= 1:
        return []

    if axis == "x":
        mid = _split_point(cell.x0, cell.x1)
        return [
            Cell(cell.x0, cell.y0, cell.t0, mid, cell.y1, cell.t1),
            Cell(mid + 1, cell.y0, cell.t0, cell.x1, cell.y1, cell.t1),
        ]
    if axis == "y":
        mid = _split_point(cell.y0, cell.y1)
        return [
            Cell(cell.x0, cell.y0, cell.t0, cell.x1, mid, cell.t1),
            Cell(cell.x0, mid + 1, cell.t0, cell.x1, cell.y1, cell.t1),
        ]
    mid = cell.t0 + (cell.t1 - cell.t0) // 2
    return [
        Cell(cell.x0, cell.y0, cell.t0, cell.x1, cell.y1, mid),
        Cell(cell.x0, cell.y0, mid, cell.x1, cell.y1, cell.t1),
    ]


def make_tiles(root: Cell, tiles: int) -> list[Cell]:
    """把根单元切成 tiles 个顶层大区（仅沿空间轴二分，时间窗保持完整）。

    tiles 会被收敛到不超过根单元空间点数的 2 的幂；这样每个大区大小尽量
    均衡，后续调度按大区轮询，避免单个高密度区域独占预算。
    """
    if tiles < 1:
        raise ValueError("tiles 必须 >= 1")
    result = [root]
    while len(result) < tiles:
        # 选当前可沿空间拆分的、最长边最大的大区，保持空间均衡。
        candidate = None
        for cell in result:
            longest = max(cell.x_width, cell.y_width)
            if longest <= 1:
                continue
            if candidate is None or longest > max(candidate.x_width, candidate.y_width):
                candidate = cell
        if candidate is None:
            break
        result.remove(candidate)
        if candidate.x_width >= candidate.y_width and candidate.x_width > 1:
            mid = _split_point(candidate.x0, candidate.x1)
            result.append(Cell(candidate.x0, candidate.y0, candidate.t0,
                               mid, candidate.y1, candidate.t1))
            result.append(Cell(mid + 1, candidate.y0, candidate.t0,
                               candidate.x1, candidate.y1, candidate.t1))
        else:
            mid = _split_point(candidate.y0, candidate.y1)
            result.append(Cell(candidate.x0, candidate.y0, candidate.t0,
                               candidate.x1, mid, candidate.t1))
            result.append(Cell(candidate.x0, mid + 1, candidate.t0,
                               candidate.x1, candidate.y1, candidate.t1))
    result.sort(key=Cell.key)
    return result
