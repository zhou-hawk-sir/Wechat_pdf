"""微信 4.0 数据库解密。

微信 4.0 的 db_storage 下的库采用 SQLCipher 4 兼容的加密：
- 页大小 4096 字节，保留区 80 字节（16 IV + 64 HMAC）。
- 第 1 页前 16 字节为盐，其余页无盐。
- AES-256-CBC，密钥为 32 字节原始密钥。
- 校验密钥时用第 1 页的 HMAC-SHA512 与 SQLite 头。
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from pathlib import Path

from Crypto.Cipher import AES

PAGE_SIZE = 4096
KEY_SIZE = 32
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE_SIZE = IV_SIZE + HMAC_SIZE  # 80
MAC_SALT_XOR = 0x3A
MAC_ITERATIONS = 2
MAC_KEY_LENGTH = 32
SQLITE_HEADER = b"SQLite format 3\x00"


def derive_mac_key(key: bytes, salt: bytes) -> bytes:
    """由 AES 密钥与库盐派生 HMAC 校验密钥。"""
    mac_salt = bytes(b ^ MAC_SALT_XOR for b in salt)
    return hashlib.pbkdf2_hmac("sha512", key, mac_salt, MAC_ITERATIONS, dklen=MAC_KEY_LENGTH)


def _page_iv(page: bytes) -> bytes:
    start = PAGE_SIZE - RESERVE_SIZE
    return page[start : start + IV_SIZE]


def _decrypt_page(key: bytes, page: bytes, first: bool) -> bytes:
    iv = _page_iv(page)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    if first:
        payload = page[SALT_SIZE : PAGE_SIZE - RESERVE_SIZE]
        return SQLITE_HEADER + cipher.decrypt(payload) + b"\x00" * RESERVE_SIZE
    payload = page[: PAGE_SIZE - RESERVE_SIZE]
    return cipher.decrypt(payload) + b"\x00" * RESERVE_SIZE


def verify_key(key: bytes, src: str | Path) -> bool:
    """校验密钥是否匹配给定数据库文件（第 1 页 HMAC + SQLite 头）。"""
    src = Path(src)
    with src.open("rb") as f:
        page = f.read(PAGE_SIZE)
    if len(page) < PAGE_SIZE:
        page = page.ljust(PAGE_SIZE, b"\x00")

    salt = page[:SALT_SIZE]
    mac_key = derive_mac_key(key, salt)
    # HMAC 覆盖密文(4000) + IV(16)，并以小端 uint32 追加页号 1
    data = page[SALT_SIZE : PAGE_SIZE - HMAC_SIZE] + struct.pack("<I", 1)
    expected = hmac.new(mac_key, data, hashlib.sha512).digest()
    if not hmac.compare_digest(expected, page[PAGE_SIZE - HMAC_SIZE :]):
        return False

    plain = _decrypt_page(key, page, first=True)
    return plain.startswith(SQLITE_HEADER)


def decrypt_file(key: bytes, src: str | Path, dst: str | Path, merge_wal: bool = True) -> None:
    """把加密库逐页解密写入目标文件。

    微信在运行时会用 WAL 模式，最近的改动可能还留在 ``-wal`` 里。``merge_wal``
    为真时会把已提交的 WAL 帧合并进来，再校验一次；校验失败就退回只看主库。
    """
    src = Path(src)
    dst = Path(dst)
    file_size = src.stat().st_size
    page_count = (file_size + PAGE_SIZE - 1) // PAGE_SIZE

    with src.open("rb") as fin, dst.open("wb") as fout:
        for index in range(page_count):
            page = fin.read(PAGE_SIZE)
            if len(page) < PAGE_SIZE:
                page = page.ljust(PAGE_SIZE, b"\x00")
            fout.write(_decrypt_page(key, page, first=(index == 0)))

    if merge_wal and _apply_wal(key, src, dst) is False:
        # 合并后库坏了：重写一份干净的，宁可少几条也不要给出坏库
        decrypt_file(key, src, dst, merge_wal=False)


# WAL 文件头魔数，两个取值代表校验和的字节序。
_WAL_MAGICS = (0x377F0682, 0x377F0683)
_WAL_HEADER_SIZE = 32
_WAL_FRAME_HEADER_SIZE = 24


def _read_wal_frames(wal: Path) -> tuple[int, list[tuple[int, int, bytes]]]:
    """读出 WAL 里的帧，返回 ``(页大小, [(页号, 提交页数, 加密页)])``。"""
    try:
        data = wal.read_bytes()
    except OSError:
        return 0, []
    if len(data) < _WAL_HEADER_SIZE:
        return 0, []
    if struct.unpack(">I", data[:4])[0] not in _WAL_MAGICS:
        return 0, []
    page_size = struct.unpack(">I", data[8:12])[0]
    if page_size != PAGE_SIZE:
        return 0, []

    frames: list[tuple[int, int, bytes]] = []
    offset = _WAL_HEADER_SIZE
    step = _WAL_FRAME_HEADER_SIZE + page_size
    while offset + step <= len(data):
        header = data[offset : offset + _WAL_FRAME_HEADER_SIZE]
        page_no, commit_size = struct.unpack(">II", header[:8])
        frames.append((page_no, commit_size, data[offset + _WAL_FRAME_HEADER_SIZE : offset + step]))
        offset += step
    return page_size, frames


def _apply_wal(key: bytes, src: Path, dst: Path) -> bool:
    """把 WAL 中已提交的页写进解密结果；返回是否成功。"""
    _, frames = _read_wal_frames(src.with_name(src.name + "-wal"))
    if not frames:
        return True

    last_commit_pages = 0
    try:
        with dst.open("r+b") as fh:
            for page_no, commit_size, raw in frames:
                if page_no < 1:
                    continue
                plain = _decrypt_page(key, raw, first=(page_no == 1))
                fh.seek((page_no - 1) * PAGE_SIZE)
                fh.write(plain)
                if commit_size:
                    last_commit_pages = commit_size
            if last_commit_pages:
                fh.truncate(last_commit_pages * PAGE_SIZE)
    except OSError:
        return False

    return _quick_check(dst)


def _quick_check(path: Path) -> bool:
    """用 SQLite 自检确认解密结果可读。"""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("PRAGMA quick_check(1)").fetchone()
        return bool(row) and str(row[0]).lower() == "ok"
    except sqlite3.Error:
        return False
    finally:
        conn.close()
