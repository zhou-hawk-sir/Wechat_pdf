"""PDF 导出器：调用系统浏览器的无头模式把 HTML 打印成 PDF。

不依赖 weasyprint / GTK —— Windows 上装 GTK 对普通用户来说成本过高。
本机装了 Chrome 或 Edge 即可，两者命令行兼容。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from .base import ExportContext, Exporter, notify
from .html import render_html

# 相对各安装根目录的常见位置，前两个是系统级安装，后两个是用户级安装
_BROWSER_SUBPATHS = (
    r"Google\Chrome\Application\chrome.exe",
    r"Microsoft\Edge\Application\msedge.exe",
)

# 窗口版程序没有控制台，直接起浏览器会弹黑框一闪而过；带上标志静默运行
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _install_dirs() -> list[Path]:
    """浏览器可能的安装根目录。

    非管理员安装的 Chrome 只落在 ``%LOCALAPPDATA%``，只查 Program Files 会漏。
    """
    dirs: list[Path] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        value = os.environ.get(env)
        if value:
            dirs.append(Path(value))
    return dirs


def _registry_browsers() -> list[str]:
    """从注册表 App Paths 读浏览器路径，覆盖装到自定义目录的情况。"""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - 仅非 Windows 会走到
        return []
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    found: list[str] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for name in ("chrome.exe", "msedge.exe"):
            try:
                with winreg.OpenKey(hive, rf"{base}\{name}") as key:
                    value = winreg.QueryValueEx(key, "")[0]
            except OSError:
                continue
            if isinstance(value, str) and Path(value).exists():
                found.append(value)
    return found


def _candidates() -> list[str]:
    """按优先级列出所有可用的浏览器路径，Chrome 排在 Edge 前面。"""
    found: list[str] = []
    for root in _install_dirs():
        for sub in _BROWSER_SUBPATHS:
            path = root / sub
            if path.exists():
                found.append(str(path))
    found += _registry_browsers()
    for name in ("chrome.exe", "msedge.exe"):
        which = shutil.which(name)
        if which:
            found.append(which)
    # ProgramFiles 与 ProgramW6432 常指向同一目录，注册表也会返回同样的路径
    unique = list(dict.fromkeys(found))
    # 旧版 Edge（EdgeHTML）不认 --headless=new，能选 Chrome 就先选 Chrome
    unique.sort(key=lambda p: 0 if "chrome" in Path(p).name.lower() else 1)
    return unique


def find_browser() -> str | None:
    """返回可用的 Chrome / Edge 可执行文件路径。"""
    candidates = _candidates()
    return candidates[0] if candidates else None


def _blank_info_string(data: bytearray, key: bytes) -> bool:
    """把 Info 字典里 ``/key (value)`` 的值用等长空格覆盖。

    等长覆盖不动对象偏移量，xref 表仍然有效，不需要重写整个 PDF。
    """
    head = b"/" + key + b" ("
    start = data.find(head)
    if start < 0:
        return False
    start += len(head)
    end = start
    depth = 1
    while end < len(data):
        char = data[end : end + 1]
        if char == b"\\":
            end += 2
            continue
        if char == b"(":
            depth += 1
        elif char == b")":
            depth -= 1
            if depth == 0:
                break
        end += 1
    if end >= len(data):
        return False
    data[start:end] = b" " * (end - start)
    return True


def strip_pdf_metadata(pdf_path: Path) -> None:
    """清掉浏览器打印时写进 PDF 的软件标识（UA、渲染引擎版本）。

    只处理 Info 里的 Creator / Producer 两项，文档属性里的时间与标题保留。
    """
    path = Path(pdf_path)
    try:
        data = bytearray(path.read_bytes())
    except OSError:
        return
    changed = _blank_info_string(data, b"Creator")
    changed = _blank_info_string(data, b"Producer") or changed
    if changed:
        try:
            path.write_bytes(data)
        except OSError:
            pass


def _time_budget(html_path: Path) -> int:
    """按图片数量估算渲染预算：图片多时固定 8 秒不够，会打出缺图的 PDF。"""
    try:
        text = Path(html_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 8000
    images = text.count("<img")
    return max(8000, min(60000, 5000 + images * 200))


def _is_elevated() -> bool:
    """当前进程是否以管理员身份运行。"""
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 查不出来就当作普通权限
        return False


# 正常打印也会写进日志的噪音，不能当作失败原因
_LOG_NOISE = (
    "direct_composition_support",
    "GetGpuDriverOverlayInfo",
    "QQBrowser",
    "registration_response",
    "GetVSyncParametersIfAvailable",
)

# 真正跟打印失败相关的关键词，优先展示
_LOG_KEY = (
    "Printing failed",
    "FILE_ERROR",
    "Access is denied",
    "could not open",
    "Failed to write",
    "Failed opening",
    "Cannot write",
)


def _log_hint(profile: str) -> str:
    """从 chrome_debug.log 里挑出与打印失败相关的 ERROR 行。"""
    log = Path(profile) / "chrome_debug.log"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [
        line.strip()
        for line in text.splitlines()
        if "ERROR" in line and not any(noise in line for noise in _LOG_NOISE)
    ]
    if not lines:
        return ""
    picked = [ln for ln in lines if any(key in ln for key in _LOG_KEY)] or lines
    return " | ".join(picked[-2:])[:400]


def _print_once(html_path: Path, target: Path, browser: str, timeout: int) -> tuple[bool, str]:
    """用一个浏览器打印一次，返回 (是否成功, 失败原因)。"""
    profile = tempfile.mkdtemp(prefix="pdfprint_")
    # as_uri 会把中文、空格、# 正确转义，手拼 file:/// 在这些字符上会打出空白页
    url = Path(html_path).resolve().as_uri()
    common = [
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--enable-logging",
        "--run-all-compositor-stages-before-draw",
        "--allow-file-access-from-files",
        f"--virtual-time-budget={_time_budget(html_path)}",
        f"--user-data-dir={profile}",
        f"--print-to-pdf={target.resolve()}",
        url,
    ]
    reason = ""
    try:
        # 新版优先，失败退回老参数，兼容 111 之前的 Chrome 与 EdgeHTML 之后的 Edge
        for mode in ("--headless=new", "--headless"):
            try:
                done = subprocess.run(
                    [browser, mode, *common],
                    capture_output=True,
                    timeout=timeout,
                    stdin=subprocess.DEVNULL,
                    creationflags=_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                reason = str(exc)
                break
            if target.exists() and target.stat().st_size > 0:
                return True, ""
            detail = (done.stderr or b"").decode("utf-8", "replace").strip()
            reason = (
                _log_hint(profile)
                or detail[-300:]
                or f"浏览器没有任何输出，返回码 {done.returncode}"
            )
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return False, reason


def html_to_pdf(
    html_path: Path,
    pdf_path: Path,
    browsers: list[str] | str,
    timeout: int = 1800,
    on_try: Callable[[str], None] | None = None,
) -> None:
    """用浏览器无头模式把本地 HTML 打印成 PDF。

    传入多个浏览器时会逐个尝试，某个不可用就换下一个 —— 只装 Edge 的机器、
    Chrome 被策略禁用的机器都能出结果。

    打印前会删掉同名旧文件，否则上一次的残留会被误判成本次成功。

    注意：浏览器不要以管理员身份运行。Chrome 检测到自己被提权时会降权重启，
    父进程拿到的返回码是 0、stderr 是空的、文件也不会生成，跟成功无法区分。
    """
    target = Path(pdf_path)
    if target.exists():
        try:
            target.unlink()
        except OSError as exc:
            raise RuntimeError(f"目标文件被占用，请先关闭它：{target}") from exc

    candidates = [browsers] if isinstance(browsers, str) else list(browsers)
    if not candidates:
        raise RuntimeError("未找到 Chrome 或 Edge，无法生成 PDF；可改选 HTML 格式")

    failures: list[str] = []
    for browser in candidates:
        if on_try is not None:
            on_try(Path(browser).name)
        ok, reason = _print_once(html_path, target, browser, timeout)
        if ok:
            strip_pdf_metadata(target)
            return
        # 失败时可能留下 0 字节文件，清掉，免得下次被误判
        if target.exists() and target.stat().st_size == 0:
            target.unlink(missing_ok=True)
        failures.append(f"{Path(browser).name}：{reason}")

    hint = ""
    if _is_elevated():
        hint = "；本程序当前以管理员身份运行，浏览器的无头打印在这种权限下不工作，请用普通权限重新打开本工具"
    detail = "；".join(failures)
    raise RuntimeError(
        f"PDF 生成失败（已尝试 {len(candidates)} 个浏览器）——{detail}{hint}；"
        "若始终失败，可改选 HTML 格式，HTML 不需要浏览器"
    )


class PdfExporter(Exporter):
    name = "pdf"
    extension = "pdf"

    def export(self, ctx: ExportContext):
        messages = list(ctx.messages)
        notify(ctx, 0.0, f"正在生成 PDF 页面：{len(messages)} 条消息 …")
        page = render_html(ctx.display_name, messages)
        pdf_path = self.output_path(ctx)
        # 打印用的 HTML 必须放在输出目录里，图片/语音才能按相对路径加载成功
        html_path = ctx.out_dir / f".{pdf_path.stem}.print.html"
        try:
            browsers = _candidates()
            if not browsers:
                raise RuntimeError(
                    "未找到 Chrome 或 Edge，无法生成 PDF；"
                    "可改选 HTML 格式，或先安装浏览器"
                )
            html_path.write_text(page, encoding="utf-8")
            # 打印这一步没法预估耗时，交给界面显示不确定进度
            notify(
                ctx,
                -1,
                f"正在打印 PDF：{len(messages)} 条消息，图片多时需要等一会儿 …",
            )
            html_to_pdf(
                html_path,
                pdf_path,
                browsers,
                on_try=lambda name: notify(ctx, -1, f"正在用 {name} 打印 PDF …"),
            )
            return self._result(ctx, pdf_path, len(messages))
        except Exception as exc:  # noqa: BLE001 - 导出失败需记录并继续
            # 打不出 PDF 时至少留一份能直接看的 HTML，别让人两手空空
            note = ""
            kept = ctx.out_dir / f"{pdf_path.stem}.html"
            try:
                if not kept.exists():
                    kept.write_text(page, encoding="utf-8")
                    note = f"；已改为生成 {kept.name}，用浏览器打开后按 Ctrl+P 可另存为 PDF"
            except OSError:
                pass
            return self._result(ctx, None, 0, error=f"{exc}{note}")
        finally:
            html_path.unlink(missing_ok=True)
