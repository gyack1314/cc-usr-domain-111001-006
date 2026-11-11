"""开发检查入口，可在隔离模式下运行：python3 -I run_checks.py。

显式把仓库根目录加入 sys.path，避免依赖 -I 下被关闭的隐式工作目录路径。
"""
import os
import sys
import unittest

if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(root, "tests"), top_level_dir=root)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
