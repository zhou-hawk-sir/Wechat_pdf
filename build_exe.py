"""打包成单文件 exe：``python build_exe.py``。

产物在 ``dist/聊天记录导出.exe``，双击即可运行，目标机器不需要装 Python。
打包机需要先 ``pip install -e ".[dev]"``（含 pyinstaller）。

可选参数指定输出目录，用于 dist 里的旧 exe 正在运行时换个位置打包：
``python build_exe.py dist_new``
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "聊天记录导出"


def main() -> int:
    try:
        import PyInstaller.__main__  # noqa: F401
    except ImportError:
        print("缺少 pyinstaller：先执行 pip install pyinstaller")
        return 1

    dist = ROOT / sys.argv[1] if len(sys.argv) > 1 else ROOT / "dist"
    work = ROOT / f"build_{dist.name}"
    for folder in (work, dist):
        shutil.rmtree(folder, ignore_errors=True)

    PyInstaller.__main__.run(
        [
            str(ROOT / "main.py"),
            "--name",
            NAME,
            "--onefile",
            "--windowed",
            "--noconfirm",
            "--clean",
            "--paths",
            str(ROOT / "src"),
            # pilk 是 C 扩展（含 .pyd），必须整包收进去
            "--collect-all",
            "pilk",
            "--hidden-import",
            "Crypto.Cipher.AES",
            "--hidden-import",
            "zstandard",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            "--specpath",
            str(work),
        ]
    )
    exe = dist / f"{NAME}.exe"
    if not exe.exists():
        print("打包失败：没有生成 exe")
        return 1
    print(f"已生成 {exe}（{exe.stat().st_size / 1048576:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
