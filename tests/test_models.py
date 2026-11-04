"""数据模型与类型映射的单元测试。"""

from wechat_log.core.models import Contact, Message, message_kind


def test_message_kind_mapping():
    cases = {
        1: "text",
        3: "image",
        34: "voice",
        43: "video",
        47: "emoji",
        49: "app",
        50: "call",
        10000: "system",
        999: "other",
    }
    for code, kind in cases.items():
        assert message_kind(code) == kind, code


def test_message_kind_masks_subtype():
    # 微信 4.0 把子类型编码在高 32 位：(51 << 32) | 49 仍是 app 消息。
    assert message_kind((51 << 32) | 49) == "app"
    assert message_kind((2000 << 32) | 49) == "app"


def test_message_kind_property():
    msg = Message(
        local_id=1, server_id=1, create_time=0, msg_type=3, talker="t", sender="s", is_sender=True
    )
    assert msg.kind == "image"


def test_contact_display_name_prefers_remark():
    c = Contact(user_name="wxid_x", nickname="昵称", remark="备注")
    assert c.display_name == "备注"


def test_contact_display_name_falls_back():
    c = Contact(user_name="wxid_x")
    assert c.display_name == "wxid_x"
