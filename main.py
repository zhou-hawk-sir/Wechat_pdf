"""打包入口：PyInstaller 会把它当独立脚本执行，不能用相对导入。"""

from __future__ import annotations

import sys

from wechat_log.app import main

if __name__ == "__main__":
    sys.exit(main())
