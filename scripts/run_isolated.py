#!/usr/bin/env python3
"""隔离模式运行器：以 python -I 语义启动 CLI。

用途：导入来自外部、同契约的新证据文件时，外部文件应放在独立目录，
使用本运行器可避免解释器从当前目录或脚本目录加载被植入的同名模块
（例如伪造的 json.py）。仓库内受信任的 inputs/ 用普通方式运行即可。

用法：
    python3 scripts/run_isolated.py <普通 CLI 参数...>
例如：
    python3 scripts/run_isolated.py import-evidence \
        --workdir work --file /data/incoming/extra_evidence.json
"""

import os
import sys

if not sys.flags.isolated:
    # 重新以隔离模式执行自身：忽略 PYTHONPATH/PYTHON* 与用户站点包
    os.execv(sys.executable, [sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])

# 隔离模式下 sys.path 不含当前工作目录，需显式加入仓库根目录
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from emergency_prep.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
