"""采集器命令行入口，可在隔离模式下运行：

    python3 -I harvest.py --bbox 0,0,10,10 --time 0,5 --budget 20 --out state/run1

普通模式下等价的包入口仍然可用：

    python3 -m harvester --bbox ... --time ... --budget ... --out ...
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from harvester.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
