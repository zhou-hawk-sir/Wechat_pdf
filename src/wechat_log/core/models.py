"""领域数据模型。

所有模型均为纯数据类，不包含 IO 逻辑，方便各层复用与单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Account:
    """一个微信账号（对应 xwechat_files 下的 wxid_* 目录）。"""

    wxid: str
    root: Path
    nickname: str = ""

    @property
    def display_name(self) -> str:
        return self.nickname or self.wxid


@dataclass
class Contact:
    """联系人（好友或群）。"""

    user_name: str
    nickname: str = ""
    remark: str = ""
    alias: str = ""
    is_group: bool = False

    @property
    def display_name(self) -> str:
        return self.remark or self.nickname or self.user_name


@dataclass
class Session:
    """一次会话（与某个联系人/群的聊天）。"""

    user_name: str
    create_time: int = 0
    order: int = 0


@dataclass
class Media:
    """一条消息引用的媒体附件。"""

    kind: str
    source: Path | None = None
    thumb: Path | None = None
    # 建议的文件名（原始文件名，或按消息时间生成的名称）。
    name: str = ""


@dataclass
class Message:
    """一条聊天消息。"""

    local_id: int
    server_id: int
    create_time: int
    msg_type: int
    talker: str
    sender: str
    is_sender: bool
    content: str = ""
    media: Media | None = None
    # 原始 message_content（明文字符串或 ZSTD 压缩字节），供媒体解析使用。
    raw_content: object = None

    @property
    def kind(self) -> str:
        return message_kind(self.msg_type)


@dataclass
class ExportResult:
    """单个会话、单个格式的导出结果。"""

    session_user_name: str
    format: str
    ok: bool
    path: Path | None = None
    message_count: int = 0
    error: str = ""


@dataclass
class ExportReport:
    """一次导出任务的汇总。"""

    results: list[ExportResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)


# 微信消息类型常见取值。微信 4.0 把子类型编码在高 32 位，
# 实际类型 = local_type 的低 32 位（例如 (51 << 32) | 49 表示子类型 51 的 app 消息）。
_BASE_MASK = 0xFFFFFFFF

_TYPE_NAMES = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "emoji",
    49: "app",
    50: "call",
    10000: "system",
}


def message_kind(msg_type: int) -> str:
    """把微信消息类型数字映射为简短类别名。"""
    return _TYPE_NAMES.get(msg_type & _BASE_MASK, "other")
