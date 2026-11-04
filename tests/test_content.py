"""消息内容解析的单元测试。"""

import zstandard

from wechat_log.core import content


def _zstd(data: bytes) -> bytes:
    return zstandard.ZstdCompressor().compress(data)


def test_message_text_plain_text():
    assert content.message_text(1, "你好") == "你好"


def test_message_text_plain_text_bytes():
    assert content.message_text(1, "你好".encode()) == "你好"


def test_message_text_compressed_text():
    # 长文本 / 引用回复会把正文也做 ZSTD 压缩。
    compressed = _zstd("长文本内容".encode())
    assert content.message_text(1, compressed) == "长文本内容"


def test_message_text_image_placeholder():
    assert content.message_text(3, b"") == "[图片]"


def test_message_text_app_extracts_title_and_url():
    xml = b'<?xml version="1.0"?><msg><appmsg><title>\xe9\x93\xbe\xe6\x8e\xa5</title>'
    xml += b"<des /><url>https://example.com</url></appmsg></msg>"
    compressed = _zstd(xml)
    text = content.message_text((51 << 32) | 49, compressed)
    assert "链接" in text
    assert "https://example.com" in text


def test_message_xml_returns_none_for_text():
    assert content.message_xml("plain text") is None


def test_message_xml_decompresses():
    xml = b'<msg><img md5="abc" /></msg>'
    assert content.message_xml(_zstd(xml)) == '<msg><img md5="abc" /></msg>'
