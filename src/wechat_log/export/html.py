"""HTML 导出器：微信气泡样式的时间线网页。

同一份 HTML 也是 PDF 的渲染源（见 :mod:`wechat_log.export.pdf`），所以样式都
集中在 ``_STYLE`` 里，两边保持一致。
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import datetime

from jinja2 import Environment

from wechat_log.core.models import Message

from .base import ExportContext, Exporter, format_time, media_path

_STYLE = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body {
  margin:0; background:#ededed; color:#1a1a1a;
  font-family:"Microsoft YaHei","微软雅黑","PingFang SC","Segoe UI",sans-serif;
  font-size:13px; line-height:1.6;
}
.page { max-width:900px; margin:0 auto; padding:24px 18px; }
.cover { padding:56px 0 36px; text-align:center; }
.cover h1 { font-size:24px; margin:0 0 8px; font-weight:600; }
.cover .sub { color:#888; font-size:12px; }
.cover table { margin:22px auto 0; font-size:12px; color:#555; }
.cover td { padding:3px 10px; }
.cover td:first-child { color:#999; text-align:right; }
h2.chapter {
  font-size:14px; font-weight:600; color:#333;
  border-left:4px solid #07c160; padding-left:8px; margin:26px 0 12px;
}
.day { text-align:center; margin:16px 0 10px; }
.day span {
  display:inline-block; background:rgba(0,0,0,.12); color:#fff;
  font-size:11px; padding:2px 8px; border-radius:3px;
}
.msg { display:flex; margin:8px 0; align-items:flex-start; }
.msg.self { flex-direction:row-reverse; }
.avatar {
  width:34px; height:34px; border-radius:4px; flex:0 0 34px;
  background:#c8c9cc; color:#fff; font-size:12px;
  display:flex; align-items:center; justify-content:center; overflow:hidden;
}
.msg.self .avatar { background:#8dc63f; }
.body { max-width:74%; margin:0 10px; }
.msg.self .body { text-align:right; }
.nick { font-size:11px; color:#9a9a9a; margin-bottom:3px; }
.bubble {
  position:relative; display:inline-block; text-align:left;
  background:#fff; border-radius:4px; padding:8px 11px;
  white-space:pre-wrap; word-break:break-word;
}
.msg.self .bubble { background:#95ec69; }
.sys { text-align:center; color:#9a9a9a; font-size:11px; margin:8px 0; }
.media { color:#576b95; font-size:12px; font-style:italic; }
.pic { max-width:280px; max-height:360px; display:block; border-radius:4px; }
.msg.self .pic { margin-left:auto; }
.vid { max-width:320px; display:block; border-radius:4px; }
.audio audio { height:32px; max-width:260px; vertical-align:middle; }
a.file { color:#576b95; text-decoration:none; }
.note { display:block; font-size:10px; color:#a0a0a0; margin-top:2px; }
.foot { color:#999; font-size:11px; text-align:center; padding:26px 0 8px; }
@media print { body { background:#fff; } .page { max-width:none; padding:0; } }
"""

_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{{ title }}</title>
<style>{{ style }}</style>
</head>
<body>
<div class="page">
<div class="cover">
  <h1>{{ title }}</h1>
  <div class="sub">聊天记录导出</div>
  <table>
    <tr><td>对象</td><td>{{ peer }}</td></tr>
    <tr><td>消息条数</td><td>{{ count }}</td></tr>
    <tr><td>时间范围</td><td>{{ span }}</td></tr>
    <tr><td>导出时间</td><td>{{ now }}</td></tr>
    {% if restored %}<tr><td>已还原</td><td>{{ restored }}</td></tr>{% endif %}
  </table>
</div>
<h2 class="chapter">聊天记录</h2>
{% for m in messages %}
{{ m }}
{% endfor %}
<div class="foot">{{ now }}</div>
</div>
</body>
</html>
"""

_ENV = Environment(autoescape=False)


def _escape(value: str) -> str:
    return html.escape(value or "")


def _media_block(message: Message) -> str | None:
    """渲染媒体块；无法渲染时返回 ``None`` 交给文字兜底。"""
    media = message.media
    if media is None or media.source is None:
        return None
    src = _escape(media_path(media))
    name = _escape(media.name or "")
    if media.kind == "image":
        return f'<div class="bubble"><img class="pic" src="{src}" alt="图片"></div>'
    if media.kind == "voice":
        if (media.name or "").lower().endswith(".silk"):
            return (
                '<div class="bubble"><span class="media">语音</span>'
                f'<span class="note">{name}（原始 SILK 编码，需专用播放器）</span></div>'
            )
        return (
            '<div class="bubble"><span class="audio">'
            f'<audio controls preload="none" src="{src}"></audio></span></div>'
        )
    if media.kind == "video":
        return (
            f'<div class="bubble"><video class="vid" controls preload="none" '
            f'src="{src}"></video><span class="note">{name}</span></div>'
        )
    return f'<div class="bubble"><a class="file" href="{src}">附件：{name}</a></div>'


def _message_block(message: Message) -> str:
    if message.kind == "system":
        return f'<div class="sys">{_escape(message.content)}</div>'
    who = "我" if message.is_sender else (message.sender or "未知")
    css = "msg self" if message.is_sender else "msg"
    body = _media_block(message)
    if body is None:
        text = message.content or ""
        if not text:
            return ""
        if text.startswith("["):
            body = f'<div class="bubble"><span class="media">{_escape(text)}</span></div>'
        else:
            body = f'<div class="bubble">{_escape(text)}</div>'
    return (
        f'<div class="{css}">'
        f'<div class="avatar">{_escape(who[:2])}</div>'
        f'<div class="body"><div class="nick">{_escape(who)}  '
        f"{format_time(message.create_time)}</div>{body}</div>"
        "</div>"
    )


def _day_separator(timestamp: int) -> str:
    try:
        text = datetime.fromtimestamp(timestamp).strftime("%Y年%m月%d日")
    except (OverflowError, OSError, ValueError):
        text = ""
    return f'<div class="day"><span>{_escape(text)}</span></div>'


def render_html(display_name: str, messages: Iterable[Message]) -> str:
    items: list[str] = []
    count = 0
    first = 0
    last = 0
    images = voices = videos = files = 0
    current_day = None
    for message in messages:
        count += 1
        if not first:
            first = message.create_time
        last = message.create_time
        day = format_time(message.create_time)[:10]
        if day and day != current_day:
            current_day = day
            items.append(_day_separator(message.create_time))
        block = _message_block(message)
        if block:
            items.append(block)
        if message.media is not None and message.media.source is not None:
            kind = message.media.kind
            images += kind == "image"
            voices += kind == "voice"
            videos += kind == "video"
            files += kind == "file"

    span = "-"
    if first:
        start = format_time(first)[:10]
        end = format_time(last)[:10]
        span = start if start == end else f"{start} ~ {end}"

    restored = [
        f"{label} {value}"
        for label, value in (("图片", images), ("语音", voices), ("视频", videos), ("附件", files))
        if value
    ]

    template = _ENV.from_string(_TEMPLATE)
    return template.render(
        title=_escape(display_name) or "聊天记录",
        peer=_escape(display_name),
        style=_STYLE,
        count=count,
        span=span,
        now=datetime.now().strftime("%Y-%m-%d %H:%M"),
        restored=" / ".join(restored),
        messages=items,
    )


class HtmlExporter(Exporter):
    name = "html"
    extension = "html"

    def export(self, ctx: ExportContext):
        try:
            messages = list(ctx.messages)
            page = render_html(ctx.display_name, messages)
            path = self.output_path(ctx)
            path.write_text(page, encoding="utf-8")
            return self._result(ctx, path, len(messages))
        except Exception as exc:  # noqa: BLE001 - 导出失败需记录并继续
            return self._result(ctx, None, 0, error=str(exc))
