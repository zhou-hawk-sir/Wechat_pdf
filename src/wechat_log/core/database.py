"""读取解密后的微信 4.0 数据库。

微信 4.0 的结构：

* ``contact/contact.db`` —— ``contact`` 表存好友/群，``stranger`` 存非常用联系人。
* ``session/session.db`` —— ``SessionTable`` 存会话列表。
* ``message/message_{0..3}.db`` —— 每个会话一张 ``Msg_<md5(username)>`` 表，
  另有 ``Name2Id`` 把数字 ID 映射回用户名（``real_sender_id`` 即其 rowid）。

这里按运行时自省读取列名，避免硬编码导致的小版本差异问题。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from . import content
from .models import Contact, Message, Session

# 群聊消息正文有时会带一行发送者标识前缀（``wxid_xxx:``），导出时应剥掉。
_GROUP_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{5,}:[ \t]*\r?\n?")

# 各逻辑字段的候选列名（大小写不敏感，按优先级排列）。
_USER_NAME_COLS = ("UserName", "user_name", "username")
_NICKNAME_COLS = ("NickName", "nick_name", "nickname")
_REMARK_COLS = ("Remark", "remark")
_ALIAS_COLS = ("Alias", "alias")
_TYPE_COLS = ("Type", "type", "local_type")
_CREATE_TIME_COLS = ("CreateTime", "create_time", "createTime")
_ORDER_COLS = ("sort_timestamp", "SortTimestamp", "Order", "order")
_LAST_TIME_COLS = ("last_timestamp", "LastTimestamp", "last_time")
_LOCAL_ID_COLS = ("local_id", "localId", "LocalId", "MsgLocalId", "id")
_SERVER_ID_COLS = ("server_id", "serverId", "ServerId", "MsgSvrID")
_CONTENT_COLS = ("message_content", "MessageContent", "Content", "StrContent")
_SENDER_COLS = ("real_sender_id", "RealSenderId", "sender_id", "SenderId", "FromUserName")


def open_decrypted(path: str | Path) -> sqlite3.Connection:
    """以只读方式打开解密后的库。"""
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return [row["name"] for row in rows]


def column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row["name"] for row in rows]


def find_column(columns: list[str], *candidates: str) -> str | None:
    """在列名列表中按候选顺序匹配第一个存在的列。"""
    lowered = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def first_table(tables: list[str], *candidates: str) -> str | None:
    """在表名列表中按候选顺序匹配第一个存在的表。"""
    lowered = {t.lower(): t for t in tables}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def load_contacts(conn: sqlite3.Connection) -> list[Contact]:
    """读取联系人（好友与群）。"""
    contacts: dict[str, Contact] = {}
    tables = table_names(conn)
    for table in ("contact", "stranger"):
        if table not in {t.lower() for t in tables}:
            continue
        cols = column_names(conn, table)
        u = find_column(cols, *_USER_NAME_COLS)
        if u is None:
            continue
        n = find_column(cols, *_NICKNAME_COLS) or ""
        r = find_column(cols, *_REMARK_COLS) or ""
        a = find_column(cols, *_ALIAS_COLS) or ""
        select = ", ".join(c for c in (u, n, r, a) if c)
        for row in conn.execute(f"SELECT {select} FROM {table}"):
            user_name = str(row[u] or "")
            if not user_name or user_name in contacts:
                continue
            contacts[user_name] = Contact(
                user_name=user_name,
                nickname=str(row[n] or "") if n else "",
                remark=str(row[r] or "") if r else "",
                alias=str(row[a] or "") if a else "",
                is_group=user_name.endswith("@chatroom"),
            )
    return list(contacts.values())


def load_sessions(conn: sqlite3.Connection) -> list[Session]:
    """读取会话列表。"""
    table = first_table(table_names(conn), "SessionTable", "Session", "session")
    if table is None:
        return []
    cols = column_names(conn, table)
    u = find_column(cols, *_USER_NAME_COLS, "SessionId")
    if u is None:
        return []
    o = find_column(cols, *_ORDER_COLS) or ""
    c = find_column(cols, *_LAST_TIME_COLS, *_CREATE_TIME_COLS) or ""

    select = ", ".join(x for x in (u, o, c) if x)
    sessions = []
    for row in conn.execute(f"SELECT {select} FROM {table}"):
        sessions.append(
            Session(
                user_name=str(row[u] or ""),
                create_time=int(row[c] or 0) if c else 0,
                order=int(row[o] or 0) if o else 0,
            )
        )
    sessions.sort(key=lambda s: s.order, reverse=True)
    return sessions


def load_name2id(conn: sqlite3.Connection) -> dict[int, str]:
    """读取 ``Name2Id`` 表，返回 ``rowid -> username`` 映射。"""
    table = first_table(table_names(conn), "Name2Id", "name2id")
    if table is None:
        return {}
    cols = column_names(conn, table)
    u = find_column(cols, *_USER_NAME_COLS, "user_name")
    if u is None:
        return {}
    mapping: dict[int, str] = {}
    for row in conn.execute(f"SELECT rowid, {u} FROM {table}"):
        user_name = str(row[u] or "")
        if user_name:
            mapping[int(row["rowid"])] = user_name
    return mapping


def message_table_name(user_name: str) -> str:
    """返回某会话用户名对应的消息表名。"""
    digest = hashlib.md5(user_name.encode("utf-8")).hexdigest()
    return f"Msg_{digest}"


def count_messages(conn: sqlite3.Connection, talker: str) -> int:
    """某会话在当前库里的消息条数，表不存在时返回 0。"""
    table = message_table_name(talker)
    tables = {t.lower() for t in table_names(conn)}
    if table.lower() not in tables:
        return 0
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0


def iter_messages(conn: sqlite3.Connection, talker: str, name2id: dict[int, str] | None = None):
    """按批次读取某会话的消息，返回 Message 生成器。"""
    name2id = name2id or {}
    table = message_table_name(talker)
    tables = {t.lower() for t in table_names(conn)}
    if table.lower() not in tables:
        return

    cols = column_names(conn, table)
    lid = find_column(cols, *_LOCAL_ID_COLS)
    sid = find_column(cols, *_SERVER_ID_COLS)
    ct = find_column(cols, *_CREATE_TIME_COLS)
    ty = find_column(cols, *_TYPE_COLS)
    sd = find_column(cols, *_SENDER_COLS)
    co = find_column(cols, *_CONTENT_COLS)
    if None in (lid, ty):
        return

    select = ", ".join(x for x in (lid, sid, ct, ty, sd, co) if x)
    batch = 2000
    offset = 0
    while True:
        rows = conn.execute(
            f"SELECT {select} FROM {table} ORDER BY {lid} LIMIT {batch} OFFSET {offset}"
        ).fetchall()
        if not rows:
            return
        for row in rows:
            local_type = int(row[ty] or 0)
            sender_id = int(row[sd] or 0) if sd else 0
            raw_content = row[co] if co else None
            text = content.message_text(local_type, raw_content)
            if talker.endswith("@chatroom"):
                text = _GROUP_PREFIX.sub("", text, count=1)
            yield Message(
                local_id=int(row[lid] or 0),
                server_id=int(row[sid] or 0) if sid else 0,
                create_time=int(row[ct] or 0) if ct else 0,
                msg_type=local_type,
                talker=talker,
                sender=name2id.get(sender_id, str(sender_id) if sender_id else ""),
                is_sender=False,
                content=text,
                raw_content=raw_content,
            )
        offset += batch
