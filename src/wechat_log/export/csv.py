"""CSV 导出器。"""

from __future__ import annotations

import csv

from .base import ExportContext, Exporter, format_time, media_path

_HEADER = ["时间", "发送者", "是否自己", "类型", "内容", "媒体路径"]


class CsvExporter(Exporter):
    name = "csv"
    extension = "csv"

    def export(self, ctx: ExportContext):
        try:
            path = self.output_path(ctx)
            count = 0
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(_HEADER)
                for m in ctx.messages:
                    media_src = media_path(m.media)
                    writer.writerow(
                        [
                            format_time(m.create_time),
                            "我" if m.is_sender else m.sender,
                            "是" if m.is_sender else "否",
                            m.kind,
                            m.content,
                            media_src,
                        ]
                    )
                    count += 1
            return self._result(ctx, path, count)
        except Exception as exc:  # noqa: BLE001
            return self._result(ctx, None, 0, error=str(exc))
