"""定位微信 4.x 数据目录与账号。

微信允许把「文件管理」目录设到任意位置，例如 ``D:\\Tencent\\Documents``，
数据就落在它下面的 ``xwechat_files``。这个路径**不写注册表**（注册表里的
``OldFileSavePath`` 是旧版微信 3.x 留下的），所以只能按名字找：

1. 环境变量 ``WECHAT_LOG_DATA_ROOT``（批量/脚本场景）
2. 上次成功找到的目录（缓存在本地，重启后秒开）
3. 常见固定位置：用户文档、各盘符根目录、注册表里的旧路径
4. 程序自己所在的路径链（exe 从微信接收目录里双击运行时能直接命中）
5. 每个固定盘按目录名做深度 3 的扫描，跳过系统目录，实测 0.1 秒级
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import Account

ROOT_NAME = "xwechat_files"
ENV_ROOT = "WECHAT_LOG_DATA_ROOT"

# 扫描时直接跳过的目录名，避免扎进系统目录和依赖树
_SCAN_SKIP = {
    "$recycle.bin",
    "$windows.~bt",
    "$windows.~ws",
    "appdata",
    "application data",
    "config.msi",
    "intel",
    "msocache",
    "node_modules",
    "perflogs",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "system volume information",
    "windows",
}


def default_data_root() -> Path:
    """微信默认的数据目录：用户文档下的 xwechat_files。"""
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    return home / "Documents" / ROOT_NAME


def is_data_root(path: str | Path | None) -> bool:
    """判断一个目录是不是微信数据根目录（而不是随便一个同名文件夹）。"""
    if not path:
        return False
    root = Path(path)
    try:
        if not root.is_dir():
            return False
        if (root / "all_users").is_dir():
            return True
        return any(
            entry.is_dir() and entry.name.startswith("wxid_")
            for entry in root.iterdir()
        )
    except OSError:
        return False


def _fixed_drives(kinds: tuple[int, ...] = (3,)) -> list[str]:
    """返回指定类型磁盘的盘符根，如 ``["C:\\", "D:\\"]``；3 是固定盘，2 是移动盘。"""
    if os.name != "nt":
        return []
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        kind_of = ctypes.windll.kernel32.GetDriveTypeW
    except (AttributeError, OSError):
        return []
    drives = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        letter = f"{chr(ord('A') + index)}:\\"
        # 网络盘和光驱不碰，挂载失效时会卡住
        if kind_of(ctypes.c_wchar_p(letter)) in kinds:
            drives.append(letter)
    return drives


def _registry_hints() -> list[Path]:
    """注册表里能读到的候选目录（含旧版微信留下的路径）。"""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    found: list[Path] = []
    for key_path in (r"Software\Tencent\Weixin", r"Software\Tencent\WeChat"):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
        except OSError:
            continue
        with key:
            for name in ("DataSavePath", "FileSavePath", "OldFileSavePath"):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if not isinstance(value, str) or not value.strip():
                    continue
                base = Path(value.strip().strip('"'))
                # 用户选的是「保存到 D:\Tencent\Documents」，微信在其下建 xwechat_files
                found += [base / ROOT_NAME, base]
                if base.name.lower() == ROOT_NAME:
                    found.append(base.parent)
    return found


def _common_hints() -> list[Path]:
    """常见安装位置，覆盖默认路径被改到文档/盘符下的情况。"""
    hints: list[Path] = [default_data_root()]
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    for folder in ("Documents", "My Documents", "Desktop"):
        hints.append(home / folder / ROOT_NAME)
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive:
        hints.append(Path(onedrive) / "Documents" / ROOT_NAME)
    for entry in ("Documents", "Tencent", "WeChat", "Weixin"):
        hints.append(home / entry / ROOT_NAME)

    for drive in _fixed_drives():
        base = Path(drive)
        hints.append(base / ROOT_NAME)
        hints.append(base / "Documents" / ROOT_NAME)
        hints.append(base / "Tencent" / ROOT_NAME)
        hints.append(base / "Tencent" / "Documents" / ROOT_NAME)
        hints.append(base / "WeChat Files" / ROOT_NAME)
        hints.append(base / "微信" / ROOT_NAME)
    return hints


def _self_hints() -> list[Path]:
    """从程序自身位置往上找：exe 常常就是从微信接收目录里双击运行的。"""
    starts: list[Path] = []
    import sys

    for value in (sys.executable, sys.argv[0] if sys.argv else ""):
        if value:
            try:
                starts.append(Path(value).resolve().parent)
            except OSError:
                continue
    try:
        starts.append(Path.cwd())
    except OSError:
        pass

    hints: list[Path] = []
    for start in starts:
        current = start
        for _ in range(8):
            if current.name.lower() == ROOT_NAME:
                hints.append(current)
                break
            hints.append(current / ROOT_NAME)
            if current.parent == current:
                break
            current = current.parent
    return hints


def _scan_drives(max_depth: int = 3) -> list[Path]:
    """在每个固定盘上按目录名扫 ``xwechat_files``，只下钻三层。"""
    found: list[Path] = []
    for drive in _fixed_drives():
        stack: list[tuple[str, int]] = [(drive, 0)]
        while stack:
            path, depth = stack.pop()
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        name = entry.name.lower()
                        if name in _SCAN_SKIP or name.startswith("$"):
                            continue
                        if name == ROOT_NAME:
                            found.append(Path(entry.path))
                            continue
                        if depth + 1 < max_depth:
                            stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return found


def _cache_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(base) / "wechat_log" / "data_root.json"


def _read_cache() -> Path | None:
    try:
        data = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    root = data.get("root") if isinstance(data, dict) else None
    if isinstance(root, str) and is_data_root(root):
        return Path(root)
    return None


def _write_cache(root: Path) -> None:
    path = _cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"root": str(root)}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _pick(paths: list[Path]) -> Path | None:
    """从候选里挑一个真正的数据目录，优先账号数据齐全的。"""
    seen: set[str] = set()
    valid: list[Path] = []
    for path in paths:
        try:
            key = str(path.resolve()).lower()
        except OSError:
            key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if is_data_root(path):
            valid.append(path)
    if not valid:
        return None
    # 有 db_storage 的账号越多，越像正在用的那个目录
    def score(root: Path) -> int:
        try:
            return sum(1 for e in root.iterdir() if (e / "db_storage").is_dir())
        except OSError:
            return 0

    valid.sort(key=lambda p: (score(p), p.stat().st_mtime if p.exists() else 0), reverse=True)
    return valid[0]


def find_data_root() -> Path | None:
    """查找微信 4.x 数据根目录，找不到返回 None。"""
    env = os.environ.get(ENV_ROOT, "").strip().strip('"')
    if env and is_data_root(env):
        return Path(env)

    cached = _read_cache()
    if cached is not None:
        return cached

    root = _pick(
        _common_hints() + _self_hints() + _registry_hints() + _scan_drives()
    )
    if root is not None:
        _write_cache(root)
        return root

    # 移动盘上放数据的情况最后再试一次，只 stat 一个路径，不会扫盘
    extra = [Path(drive) / ROOT_NAME for drive in _fixed_drives((2,))]
    root = _pick(extra)
    if root is not None:
        _write_cache(root)
    return root


def normalize_root(path: str | Path) -> Path:
    """把用户随手选的目录纠正到真正的数据根目录。

    常见选错：选了上一层（里面才有 xwechat_files）、选了某个账号目录、
    选了账号里的 db_storage。最多往上退三层。
    """
    current = Path(path)
    if is_data_root(current):
        return current
    if (current / ROOT_NAME).is_dir():
        return current / ROOT_NAME
    for _ in range(3):
        if current.parent == current:
            break
        current = current.parent
        if is_data_root(current):
            return current
    return Path(path)


def list_accounts(root: str | Path | None) -> list[Account]:
    """枚举数据根目录下的所有账号。"""
    if not root:
        return []
    root = Path(root)
    accounts = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if entry.name.startswith("wxid_") or (entry / "db_storage").is_dir():
            accounts.append(Account(wxid=entry.name, root=entry))
    return accounts


def account_db_dir(account: Account) -> Path:
    return account.root / "db_storage"


def account_msg_dir(account: Account) -> Path:
    return account.root / "msg"


def _inside_wechat_files(path: Path) -> bool:
    return any(part.lower() == ROOT_NAME for part in path.parts)


def default_output_dir() -> Path:
    """导出结果的默认位置：程序同级的「导出结果」。

    程序如果躺在微信的接收文件目录里（从微信直接双击 exe 打开时就是这样），
    不能往那儿写，改用文档目录，免得混进聊天数据里。
    """
    import sys

    try:
        base = Path(sys.argv[0]).resolve().parent
    except OSError:
        base = Path.cwd()

    candidates = [base]
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    candidates.append(home / "Documents")
    candidates.append(home)

    for folder in candidates:
        if _inside_wechat_files(folder):
            continue
        target = folder / "导出结果"
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".write_test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return target
        except OSError:
            continue
    return home / "聊天记录导出"
