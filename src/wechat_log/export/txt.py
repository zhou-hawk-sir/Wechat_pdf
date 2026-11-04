"""纯文本导出器。"""

from __future__ import annotations

from .base import ExportContext, Exporter, format_time


class TxtExporter(Exporter):
    name = "txt"
    extension = "txt"

    def export(self, ctx: ExportContext):
        try:
            path = self.output_path(ctx)
            count = 0
            with path.open("w", encoding="utf-8") as f:
                f.write(f"{ctx.display_name}\n\n")
                for m in ctx.messages:
                    who = "我" if m.is_sender else (m.sender or "未知")
                    body = m.content or (f"[{m.kind}]" if m.media else "")
                    f.write(f"[{format_time(m.create_time)}] {who}: {body}\n")
                    count += 1
            return self._result(ctx, path, count)
        except Exception as exc:  # noqa: BLE001
            return self._result(ctx, None, 0, error=str(exc))
