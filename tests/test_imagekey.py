"""图片密钥缓存的测试：换机器后仍能凭数据目录里的缓存解图。"""

import json

from wechat_log.core import imagekey
from wechat_log.core.imagekey import ImageKey, _read_cache, _write_cache


def _key(uin: int = 1023634050, wxid: str = "wxid_test") -> ImageKey:
    return ImageKey(uin=uin, wxid=wxid, aes_key=b"x" * 16)


def test_cache_lands_in_account_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    account = tmp_path / "wxid_abc"
    account.mkdir()

    _write_cache(account, "wxid_abc", _key())

    data = json.loads((account / ".wechat_log_image_key.json").read_text(encoding="utf-8"))
    assert data["account"]["uin"] == 1023634050


def test_cache_survives_path_change(tmp_path, monkeypatch):
    """账号目录换了位置（换机器/换盘符），数据目录内那份缓存仍然认。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    old = tmp_path / "旧机器" / "wxid_abc"
    old.mkdir(parents=True)
    _write_cache(old, "wxid_abc", _key())

    new = tmp_path / "新机器" / "wxid_abc"
    new.mkdir(parents=True)
    shutil_copy = (old / ".wechat_log_image_key.json").read_text(encoding="utf-8")
    (new / ".wechat_log_image_key.json").write_text(shutil_copy, encoding="utf-8")

    got = _read_cache(new, "wxid_abc")
    assert got is not None
    assert got.uin == 1023634050


def test_cache_ignores_other_account(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    account = tmp_path / "wxid_abc"
    account.mkdir()
    _write_cache(account, "wxid_abc", _key())

    assert _read_cache(account, "wxid_other") is None


def test_cache_survives_without_appdata(tmp_path, monkeypatch):
    """没有 APPDATA（拿不到 uin）时，只靠数据目录缓存也能拿到密钥。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("WECHAT_LOG_UIN", raising=False)
    account = tmp_path / "wxid_abc"
    account.mkdir()
    _write_cache(account, "wxid_abc", _key())

    assert imagekey.uin_candidates() == []
    got = _read_cache(account, "wxid_abc")
    assert got is not None and got.aes_key == b"x" * 16


def test_env_uin_comes_first(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_LOG_UIN", "1234567890")
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty"))
    assert imagekey.uin_candidates()[0] == 1234567890
