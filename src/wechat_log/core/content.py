"""消息内容解析。

微信 4.0 的消息 ``message_content`` 有两种形态：

* 文本消息（local_type 低位为 1）：直接是明文字符串。
* 其它类型：ZSTD 压缩后的微信消息 XML（帧魔数 ``28 b5 2f fd``），
  解压后形如 ``<img ...>``、``<voicemsg ...>``、``<videomsg ...>``、
  ``<appmsg>...`` 等。

本模块负责把原始内容解析成可读文本，供导出器展示。
"""

from __future__ import annotations

import re

import zstandard

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_DECOMPRESSOR = zstandard.ZstdDecompressor()


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", errors="replace")
    return str(value)


def message_xml(raw) -> str | None:
    """把压缩的 ``message_content`` 解压为 XML 字符串；不是压缩数据时返回 None。"""
    if not isinstance(raw, bytes) or not raw.startswith(_ZSTD_MAGIC):
        return None
    try:
        data = _DECOMPRESSOR.decompress(raw, max_output_size=64 * 1024 * 1024)
    except zstandard.ZstdError:
        return None
    return _as_text(data)


def _extract_tag(xml: str, name: str) -> str:
    match = re.search(rf"<{name}[^>]*>(.*?)</{name}>", xml, re.S)
    if not match:
        return ""
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", match.group(1), flags=re.S)
    return text.strip()


def _app_text(xml: str | None) -> str:
    if not xml:
        return "[链接]"
    title = _extract_tag(xml, "title")
    des = _extract_tag(xml, "des")
    url = _extract_tag(xml, "url")
    parts = [p for p in (title, des) if p]
    if not parts:
        parts.append("[链接]")
    if url and url.startswith("http"):
        parts.append(url)
    return "\n".join(parts)


def _voip_text(xml: str | None) -> str:
    if xml:
        wording = _extract_tag(xml, "diaplay_content")
        if wording:
            return wording
    return "[通话]"


def _system_text(xml: str | None) -> str:
    if not xml:
        return "[系统消息]"
    # 优先取纯文本字段，其次取 <title>，兜底返回占位。
    for name in ("wording", "content", "text", "title", "des"):
        value = _extract_tag(xml, name)
        if value:
            return value
    return "[系统消息]"


def message_text(msg_type: int, raw) -> str:
    """返回消息内容的人类可读文本。"""
    base = msg_type & 0xFFFFFFFF
    if base == 1:
        # 文本消息通常是明文，但长文本/引用回复也可能被 ZSTD 压缩。
        xml = message_xml(raw)
        if xml is not None:
            return xml
        return _as_text(raw)

    xml = message_xml(raw)
    if base == 3:
        return "[图片]"
    if base == 34:
        return "[语音]"
    if base == 43:
        return "[视频]"
    if base == 47:
        return "[表情]"
    if base == 49:
        return _app_text(xml)
    if base == 50:
        return _voip_text(xml)
    if base == 10000:
        return _system_text(xml)

    # 其它未知类型：若解压出了 XML 则尝试提取可读字段。
    if xml:
        for name in ("title", "des", "content", "wording"):
            value = _extract_tag(xml, name)
            if value:
                return value
    return ""
