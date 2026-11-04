"""导出器统一接口与公共工具。"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wechat_log.core.models import ExportResult, Message, Session

# 进度回调：0.0~1.0 表示完成比例，传 -1 表示“还在忙但估不出剩余时间”
ProgressFn = Callable[[float, str], None]


@dataclass
class ExportContext:
    """一次导出的上下文。"""

    session: Session
    display_name: str
    messages: Iterable[Message]
    out_dir: Path
    progress: ProgressFn | None = None


def notify(ctx: ExportContext, value: float, text: str = "") -> None:
    """向界面汇报进度，没接回调时什么都不做。"""
    if ctx.progress is not None:
        ctx.progress(value, text)


class Exporter:
    """导出器基类。子类实现 name / extension / export。"""

    name = "base"
    extension = ""

    def export(self, ctx: ExportContext) -> ExportResult:
        raise NotImplementedError

    def output_path(self, ctx: ExportContext) -> Path:
        return ctx.out_dir / f"{sanitize_filename(ctx.display_name)}.{self.extension}"

    def _result(
        self, ctx: ExportContext, path: Path | None, count: int, error: str = ""
    ) -> ExportResult:
        return ExportResult(
            session_user_name=ctx.session.user_name,
            format=self.name,
            ok=path is not None and not error,
            path=path,
            message_count=count,
            error=error,
        )


def media_path(media) -> str:
    """返回媒体在输出目录内的相对引用路径（统一用正斜杠，便于 HTML）。"""
    if media is None or not media.source:
        return ""
    return str(media.source).replace("\\", "/")


def sanitize_filename(name: str) -> str:
    """把显示名转成安全的文件名。"""
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name).strip().strip(".")
    return cleaned or "未命名会话"


def format_time(timestamp: int) -> str:
    """把 unix 秒转为可读时间。"""
    if not timestamp:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(timestamp)
