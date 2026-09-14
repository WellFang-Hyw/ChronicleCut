#!/usr/bin/env python
"""项目入口（免安装方式）。

    python run.py                       # 随机选题，跑全流程
    python run.py -t "赤壁之战"          # 指定题材
    python run.py plan --minutes 8      # 只写稿

等价于 `python -m hsg.cli`，区别只是这里会把 src/ 加进 sys.path，
所以不需要先 pip install。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from hsg.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
