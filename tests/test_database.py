"""数据库读取层的单元测试（用内存 SQLite 构造假表）。"""

import hashlib
import sqlite3

from wechat_log.core import database


def test_message_table_name_is_md5():
    name = database.message_table_name("wxid_abc")
    expected = "Msg_" + hashlib.md5(b"wxid_abc").hexdigest()
    assert name == expected


def test_load_contacts_reads_contact_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, alias TEXT)")
    conn.execute("INSERT INTO contact VALUES ('wxid_a', '昵称', '备注', '')")
    conn.execute("INSERT INTO contact VALUES ('123@chatroom', '群名', '', '')")
    contacts = {c.user_name: c for c in database.load_contacts(conn)}
    assert contacts["wxid_a"].display_name == "备注"
    assert contacts["123@chatroom"].is_group is True
    assert contacts["123@chatroom"].display_name == "群名"
    conn.close()


def test_load_name2id_maps_rowid():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    conn.execute("INSERT INTO Name2Id (user_name) VALUES ('wxid_me')")
    conn.execute("INSERT INTO Name2Id (user_name) VALUES ('wxid_friend')")
    mapping = database.load_name2id(conn)
    assert mapping[1] == "wxid_me"
    assert mapping[2] == "wxid_friend"
    conn.close()


def test_iter_messages_parses_text():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    table = database.message_table_name("wxid_peer")
    conn.execute(
        f"CREATE TABLE {table} "
        "(local_id INTEGER, server_id INTEGER, local_type INTEGER, create_time INTEGER, "
        "real_sender_id INTEGER, message_content TEXT)"
    )
    conn.execute(f"INSERT INTO {table} VALUES (1, 101, 1, 1700000000, 1, '你好')")
    name2id = {1: "wxid_me"}
    messages = list(database.iter_messages(conn, "wxid_peer", name2id))
    assert len(messages) == 1
    assert messages[0].content == "你好"
    assert messages[0].sender == "wxid_me"
    conn.close()


def test_group_message_prefix_is_stripped():
    """群聊正文开头的发送者标识不应出现在导出内容里。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    talker = "123@chatroom"
    table = database.message_table_name(talker)
    conn.execute(
        f"CREATE TABLE {table} "
        "(local_id INTEGER, server_id INTEGER, local_type INTEGER, create_time INTEGER, "
        "real_sender_id INTEGER, message_content TEXT)"
    )
    conn.execute(
        f"INSERT INTO {table} VALUES (1, 101, 1, 1700000000, 1, 'wxid_sender: 大家好')"
    )
    messages = list(database.iter_messages(conn, talker, {1: "wxid_sender"}))
    assert messages[0].content == "大家好"
    conn.close()
