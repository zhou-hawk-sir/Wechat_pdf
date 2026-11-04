"""解密模块的往返测试：用镜像布局加密，再解密比对。"""

import hashlib
import hmac
import os
import struct

from Crypto.Cipher import AES

from wechat_log.core import decrypt

KEY = bytes(range(32))
WRONG_KEY = bytes(range(1, 33))


def _encrypt_page(key: bytes, plain_page: bytes, first: bool, salt: bytes) -> bytes:
    """按微信 4.0 的页布局加密一页（测试镜像，与 decrypt 逻辑互逆）。"""
    assert len(plain_page) == decrypt.PAGE_SIZE
    mac_key = decrypt.derive_mac_key(key, salt)
    iv = os.urandom(decrypt.IV_SIZE)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    if first:
        payload = plain_page[decrypt.SALT_SIZE : decrypt.PAGE_SIZE - decrypt.RESERVE_SIZE]
        body = cipher.encrypt(payload)
        mac_input = body + iv + struct.pack("<I", 1)
        mac = hmac.new(mac_key, mac_input, hashlib.sha512).digest()
        return salt + body + iv + mac
    payload = plain_page[: decrypt.PAGE_SIZE - decrypt.RESERVE_SIZE]
    body = cipher.encrypt(payload)
    return body + iv + b"\x00" * decrypt.HMAC_SIZE


def _plain_page(first: bool) -> bytes:
    if first:
        return decrypt.SQLITE_HEADER + b"a" * 4000 + b"\x00" * decrypt.RESERVE_SIZE
    return b"b" * 4016 + b"\x00" * decrypt.RESERVE_SIZE


def test_decrypt_page_roundtrip_first():
    salt = os.urandom(decrypt.SALT_SIZE)
    plain = _plain_page(first=True)
    encrypted = _encrypt_page(KEY, plain, first=True, salt=salt)
    assert len(encrypted) == decrypt.PAGE_SIZE
    assert decrypt._decrypt_page(KEY, encrypted, first=True) == plain


def test_decrypt_page_roundtrip_second():
    salt = os.urandom(decrypt.SALT_SIZE)
    plain = _plain_page(first=False)
    encrypted = _encrypt_page(KEY, plain, first=False, salt=salt)
    assert decrypt._decrypt_page(KEY, encrypted, first=False) == plain


def test_verify_key_accepts_correct_key(tmp_path):
    salt = os.urandom(decrypt.SALT_SIZE)
    plain = _plain_page(first=True)
    encrypted = _encrypt_page(KEY, plain, first=True, salt=salt)
    path = tmp_path / "contact.db"
    path.write_bytes(encrypted)
    assert decrypt.verify_key(KEY, path) is True


def test_verify_key_rejects_wrong_key(tmp_path):
    salt = os.urandom(decrypt.SALT_SIZE)
    plain = _plain_page(first=True)
    encrypted = _encrypt_page(KEY, plain, first=True, salt=salt)
    path = tmp_path / "contact.db"
    path.write_bytes(encrypted)
    assert decrypt.verify_key(WRONG_KEY, path) is False


def test_decrypt_file_multi_page(tmp_path):
    salt = os.urandom(decrypt.SALT_SIZE)
    plain = _plain_page(first=True) + _plain_page(first=False) + _plain_page(first=False)
    encrypted = (
        _encrypt_page(KEY, _plain_page(first=True), first=True, salt=salt)
        + _encrypt_page(KEY, _plain_page(first=False), first=False, salt=salt)
        + _encrypt_page(KEY, _plain_page(first=False), first=False, salt=salt)
    )
    src = tmp_path / "message.db"
    src.write_bytes(encrypted)
    dst = tmp_path / "message.decrypted.db"
    decrypt.decrypt_file(KEY, src, dst)
    assert dst.read_bytes() == plain
