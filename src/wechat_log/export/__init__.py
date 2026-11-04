"""导出器注册表。"""

from __future__ import annotations

from .csv import CsvExporter
from .html import HtmlExporter
from .json import JsonExporter
from .pdf import PdfExporter
from .txt import TxtExporter

EXPORTERS = {
    "html": HtmlExporter(),
    "csv": CsvExporter(),
    "json": JsonExporter(),
    "txt": TxtExporter(),
    "pdf": PdfExporter(),
}


def get_exporter(name: str):
    return EXPORTERS.get(name)
