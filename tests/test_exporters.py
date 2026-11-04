"""导出器的单元测试（用假数据）。"""

import json
from pathlib import Path

import pytest

from wechat_log.core.models import Media, Message, Session
from wechat_log.export import get_exporter


def _messages(tmp_path: Path):
    img = tmp_path / "pic.jpg"
    img.write_bytes(b"fake-image")
    return [
        Message(
            local_id=1,
            server_id=101,
            create_time=1_700_000_000,
            msg_type=1,
            talker="friend",
            sender="friend",
            is_sender=False,
            content="你好",
        ),
        Message(
            local_id=2,
            server_id=102,
            create_time=1_700_000_060,
            msg_type=3,
            talker="friend",
            sender="me",
            is_sender=True,
            content="",
            media=Media(kind="image", source=img),
        ),
        Message(
            local_id=3,
            server_id=103,
            create_time=1_700_000_120,
            msg_type=10000,
            talker="friend",
            sender="",
            is_sender=False,
            content="系统消息",
        ),
    ]


def _ctx(tmp_path, messages):
    from wechat_log.export.base import ExportContext

    return ExportContext(
        session=Session(user_name="friend"),
        display_name="好友甲",
        messages=messages,
        out_dir=tmp_path,
    )


def test_html_exporter(tmp_path):
    exporter = get_exporter("html")
    result = exporter.export(_ctx(tmp_path, _messages(tmp_path)))
    assert result.ok
    text = result.path.read_text(encoding="utf-8")
    assert "你好" in text
    assert "pic.jpg" in text


def test_csv_exporter(tmp_path):
    result = get_exporter("csv").export(_ctx(tmp_path, _messages(tmp_path)))
    assert result.ok
    assert result.message_count == 3
    text = result.path.read_text(encoding="utf-8-sig")
    assert "你好" in text


def test_json_exporter(tmp_path):
    result = get_exporter("json").export(_ctx(tmp_path, _messages(tmp_path)))
    assert result.ok
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    assert payload["display_name"] == "好友甲"
    assert len(payload["messages"]) == 3


def test_txt_exporter(tmp_path):
    result = get_exporter("txt").export(_ctx(tmp_path, _messages(tmp_path)))
    assert result.ok
    text = result.path.read_text(encoding="utf-8")
    assert "你好" in text


def test_unknown_exporter_returns_none():
    assert get_exporter("docx") is None


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("weasyprint"),
    reason="weasyprint 未安装",
)
def test_pdf_exporter(tmp_path):
    result = get_exporter("pdf").export(_ctx(tmp_path, _messages(tmp_path)))
    assert result.ok
    assert result.path.exists()
