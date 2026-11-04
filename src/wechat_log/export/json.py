"""JSON 导出器。"""

from __future__ import annotations

import json
from datetime import datetime

from wechat_log.core.models import Message

from .base import ExportContext, Exporter, media_path


def _time_iso(timestamp: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp).isoformat()
    except (OverflowError, OSError, ValueError):
        return str(timestamp)


def _message_dict(m: Message) -> dict:
    media = None
    if m.media is not None:
        media = {"kind": m.media.kind, "path": media_path(m.media)}
    return {
        "local_id": m.local_id,
        "server_id": m.server_id,
        "create_time": m.create_time,
        "time": _time_iso(m.create_time),
        "type": m.msg_type,
        "kind": m.kind,
        "sender": m.sender,
        "is_sender": m.is_sender,
        "content": m.content,
        "media": media,
    }


class JsonExporter(Exporter):
    name = "json"
    extension = "json"

    def export(self, ctx: ExportContext):
        try:
            messages = [_message_dict(m) for m in ctx.messages]
            payload = {
                "session_user_name": ctx.session.user_name,
                "display_name": ctx.display_name,
                "messages": messages,
            }
            path = self.output_path(ctx)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return self._result(ctx, path, len(messages))
        except Exception as exc:  # noqa: BLE001
            return self._result(ctx, None, 0, error=str(exc))
