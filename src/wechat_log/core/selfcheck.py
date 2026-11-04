"""环境自检：把远程排查需要的信息一次性收集成纯文本。

用户那边出问题时，让他点一下自检、复制文本发过来，比追问十句都快。
每一项都单独兜异常，任何一处失败都不影响其余部分。
"""

from __future__ import annotations

import ctypes
import datetime
import os
import shutil
import sys
import tempfile
from pathlib import Path

WECHAT_EXE_NAMES = ("Weixin.exe", "WeChat.exe")


def _file_version(path: str) -> str:
    """读 PE 文件资源里的 FileVersion，例如 4.0.6.21。"""
    if os.name != "nt":
        return ""
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buffer):
            return ""
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
        ):
            return ""
        info = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        high, low = info[2], info[3]
        return f"{high >> 16}.{high & 0xFFFF}.{low >> 16}.{low & 0xFFFF}"
    except Exception:  # noqa: BLE001 - 读不到版本不影响自检
        return ""


def _wechat_processes() -> list[tuple[str, str, int]]:
    """返回 [(进程名, 可执行路径, PID)]。"""
    found: list[tuple[str, str, int]] = []
    try:
        import psutil
    except ImportError:
        return found
    for proc in psutil.process_iter(["name", "pid", "exe"]):
        name = proc.info.get("name") or ""
        if name.lower() in {n.lower() for n in WECHAT_EXE_NAMES}:
            found.append((name, proc.info.get("exe") or "", int(proc.info.get("pid") or 0)))
    return found


def _windows_version() -> str:
    try:
        return f"{sys.getwindowsversion().major}.{sys.getwindowsversion().build}"
    except AttributeError:
        return "非 Windows"


def _disk_free(path: Path) -> str:
    try:
        usage = shutil.disk_usage(str(path))
        return f"{usage.free / 1073741824:.1f} GB 可用 / {usage.total / 1073741824:.1f} GB"
    except OSError:
        return "读取失败"


def _writable(path: Path) -> str:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".wechat_log_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return "可写"
    except OSError as exc:
        return f"不可写（{exc.__class__.__name__}）"


def collect() -> str:
    """收集并返回自检报告。"""
    lines: list[str] = []
    add = lines.append

    from .. import __version__
    from ..export.pdf import _candidates, _is_elevated

    add("===== 聊天记录导出 环境自检 =====")
    add(f"时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    add(f"程序版本：{__version__}")
    add(f"运行方式：{'打包 exe' if getattr(sys, 'frozen', False) else '源码'}")
    exe_path = Path(sys.executable) if getattr(sys, "frozen", False) else Path(sys.argv[0])
    try:
        stamp = datetime.datetime.fromtimestamp(exe_path.stat().st_mtime)
        add(f"程序位置：{exe_path}（{stamp:%Y-%m-%d %H:%M}）")
    except OSError:
        add(f"程序位置：{exe_path}")
    elevated = _is_elevated()
    add(f"管理员权限：{'是（会导致 PDF 打印失败，请用普通权限运行）' if elevated else '否（正确）'}")
    add(f"系统：Windows {_windows_version()} / Python {sys.version.split()[0]}")

    add("")
    add("--- 微信 ---")
    procs = _wechat_processes()
    if not procs:
        add("进程：未检测到微信在运行（必须先启动并登录微信）")
    # 微信 4.x 会开好几个同名进程，按可执行文件归并，避免报告里全是重复行
    groups: dict[str, list[int]] = {}
    for name, exe, pid in procs:
        groups.setdefault(exe or name, []).append(pid)
    for exe, pids in groups.items():
        label = Path(exe).name if exe and os.path.sep in exe else exe
        add(f"进程：{label} × {len(pids)}（PID {', '.join(str(p) for p in pids)}）")
        if exe and os.path.sep in exe:
            add(f"路径：{exe}")
            version = _file_version(exe)
            add(f"版本：{version or '读取失败'}")
            if version and not version.startswith("4."):
                add("  注意：本工具针对微信 4.x，其他大版本不支持")

    try:
        from . import locate

        root = locate.find_data_root()
        if root is None:
            add("数据目录：未找到")
            add("  已尝试：环境变量、本地缓存、用户文档、注册表、各盘符根目录、深度 3 扫描")
            add("  可在界面点「数据目录」手动选择微信的 xwechat_files 文件夹")
        else:
            add(f"数据目录：{root}")
            accounts = locate.list_accounts(root)
            add(f"账号：{len(accounts)} 个")
            for acc in accounts:
                state = "有 db_storage" if (acc.root / "db_storage").is_dir() else "缺 db_storage"
                add(f"  - {acc.wxid}  {state}")
    except Exception as exc:  # noqa: BLE001
        add(f"数据目录：定位失败（{exc}）")

    add("")
    add("--- 密钥 ---")
    try:
        from .key import KeyResolver

        started = datetime.datetime.now()
        resolver = KeyResolver()
        count = len(resolver.salts) if resolver.available else 0
        cost = (datetime.datetime.now() - started).total_seconds()
        target = getattr(resolver, "process", None)
        if target is not None and hasattr(target, "pid"):
            add(f"目标进程：PID {target.pid}")
        add(f"扫描结果：{count} 组密钥，耗时 {cost:.1f} 秒")
        if count == 0:
            add("提示：扫不到密钥，通常是微信版本与本工具不匹配，或微信没有登录")
    except Exception as exc:  # noqa: BLE001
        add(f"扫描失败：{exc}")

    add("")
    add("--- 浏览器（PDF 导出用） ---")
    browsers = _candidates()
    if not browsers:
        add("未找到 Chrome 或 Edge，PDF 无法生成，可改用 HTML 格式")
    for index, path in enumerate(browsers, 1):
        add(f"{index}. {path}")
        add(f"   版本 {_file_version(path) or '读取失败'}")

    add("")
    add("--- 打印测试（真的生成一份小 PDF） ---")
    if not browsers:
        add("跳过：没有可用浏览器")
    else:
        from ..export.pdf import html_to_pdf

        work = Path(tempfile.mkdtemp(prefix="wltest_"))
        probe = work / "probe.html"
        probe.write_text(
            '<html><meta charset="utf-8"><body><p>打印测试</p></body></html>',
            encoding="utf-8",
        )
        for path in browsers[:2]:
            target = work / f"{Path(path).stem}.pdf"
            try:
                html_to_pdf(probe, target, [path], timeout=120)
                add(f"{Path(path).name}：通过（{target.stat().st_size} 字节）")
            except Exception as exc:  # noqa: BLE001 - 单个浏览器失败不影响其余
                add(f"{Path(path).name}：失败 —— {exc}")
        shutil.rmtree(work, ignore_errors=True)

    add("")
    add("--- 输出 ---")
    try:
        from . import locate

        out_dir = locate.default_output_dir()
    except Exception:  # noqa: BLE001
        out_dir = Path.home()
    add(f"默认输出目录：{out_dir}（{_writable(out_dir)}）")
    add(f"磁盘：{_disk_free(out_dir)}")
    temp = Path(os.environ.get("TEMP", "."))
    add(f"临时目录：{temp}（{_writable(temp)}）")

    add("")
    add("===== 报告结束，把以上内容整段复制发回即可 =====")
    return "\n".join(lines)
