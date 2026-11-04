"""密钥解析与候选提取的单元测试。"""

from wechat_log.core import key


def test_parse_64_hex_key():
    raw = b"x'" + b"ab" * 32 + b"'"
    assert key.parse_key_candidate(raw) == bytes.fromhex("ab" * 32)


def test_parse_96_hex_key_takes_first_32_bytes():
    raw = b"x'" + b"ab" * 48 + b"'"
    assert key.parse_key_candidate(raw) == bytes.fromhex("ab" * 32)


def test_parse_rejects_short_key():
    assert key.parse_key_candidate(b"x'" + b"ab" * 16 + b"'") is None


def test_parse_accepts_str_input():
    assert key.parse_key_candidate("x'" + "cd" * 32 + "'") == bytes.fromhex("cd" * 32)


def test_extract_candidates_dedupes():
    blob = b"garbage x'" + b"11" * 32 + b"' more x'" + b"11" * 32 + b"' x'" + b"22" * 32 + b"'"
    result = key.extract_candidates(blob)
    assert result == [bytes.fromhex("11" * 32), bytes.fromhex("22" * 32)]


def test_parse_hex_key_64_returns_key_without_salt():
    raw = "ab" * 32
    parsed_key, salt = key.parse_hex_key(raw)
    assert parsed_key == bytes.fromhex("ab" * 32)
    assert salt is None


def test_parse_hex_key_96_returns_key_and_salt():
    raw = "ab" * 32 + "cd" * 16
    parsed_key, salt = key.parse_hex_key(raw)
    assert parsed_key == bytes.fromhex("ab" * 32)
    assert salt == bytes.fromhex("cd" * 16)
