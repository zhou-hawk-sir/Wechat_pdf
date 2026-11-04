"""pytest 全局配置。"""

import os

import pytest

# GUI 测试无需真实显示器，强制 Qt 使用 offscreen 平台。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """会话级 QApplication 单例，供 GUI 测试复用。"""
    app = QApplication.instance() or QApplication([])
    yield app
