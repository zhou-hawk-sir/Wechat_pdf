"""主窗口初始化回归测试。"""

from wechat_log.ui.main_window import MainWindow


def test_window_builds_with_defaults(qapp, monkeypatch):
    # 构造窗口时会自动开始后台读取，这里屏蔽掉，只验证界面本身。
    monkeypatch.setattr(MainWindow, "start_prepare", lambda self, manual_key=None: None)
    window = MainWindow()
    assert window.format_boxes["pdf"].isChecked()
    assert window.format_boxes["html"].isChecked()
    assert window.media_box.isChecked()
    # 没有会话时导出按钮不可用，且顶部始终有一句明确的下一步提示
    assert not window.btn_export.isEnabled()
    assert window.hint.text()
    window.close()
