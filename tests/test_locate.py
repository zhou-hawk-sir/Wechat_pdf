"""定位模块的单元测试。"""

import json

from wechat_log.core import locate


def _make_root(base, name="xwechat_files", accounts=1):
    root = base / name
    (root / "all_users").mkdir(parents=True)
    for index in range(accounts):
        account = root / f"wxid_test{index}"
        (account / "db_storage").mkdir(parents=True)
    return root


def test_list_accounts_filters_wxid_dirs(tmp_path):
    (tmp_path / "wxid_abc").mkdir()
    (tmp_path / "wxid_def").mkdir()
    (tmp_path / "all_users").mkdir()
    (tmp_path / "Backup").mkdir()
    (tmp_path / "some_file.txt").write_text("x")

    accounts = locate.list_accounts(tmp_path)
    assert [a.wxid for a in accounts] == ["wxid_abc", "wxid_def"]


def test_list_accounts_empty_when_no_accounts(tmp_path):
    assert locate.list_accounts(tmp_path) == []


def test_list_accounts_none_root():
    assert locate.list_accounts(None) == []


def test_account_subdirs(tmp_path):
    account = tmp_path / "wxid_abc"
    (account / "db_storage").mkdir(parents=True)
    (account / "msg").mkdir()

    from wechat_log.core.models import Account

    acc = Account(wxid="wxid_abc", root=account)
    assert locate.account_db_dir(acc) == account / "db_storage"
    assert locate.account_msg_dir(acc) == account / "msg"


def test_is_data_root_accepts_real_layout(tmp_path):
    root = _make_root(tmp_path)
    assert locate.is_data_root(root)


def test_is_data_root_rejects_plain_folder(tmp_path):
    plain = tmp_path / "xwechat_files"
    plain.mkdir()
    (plain / "readme.txt").write_text("x")
    assert not locate.is_data_root(plain)
    assert not locate.is_data_root(tmp_path / "不存在")
    assert not locate.is_data_root(None)


def test_pick_prefers_root_with_more_accounts(tmp_path):
    small = _make_root(tmp_path / "a", accounts=1)
    big = _make_root(tmp_path / "b", accounts=3)
    assert locate._pick([small, big]) == big


def test_pick_ignores_non_root(tmp_path):
    junk = tmp_path / "xwechat_files"
    junk.mkdir()
    assert locate._pick([junk]) is None


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    root = _make_root(tmp_path / "data")
    assert locate._read_cache() is None
    locate._write_cache(root)
    assert locate.find_data_root() == root


def test_cache_ignored_when_dir_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    root = _make_root(tmp_path / "data")
    locate._write_cache(root)
    import shutil

    shutil.rmtree(root)
    assert locate._read_cache() is None


def test_env_root_wins(tmp_path, monkeypatch):
    root = _make_root(tmp_path / "data")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv(locate.ENV_ROOT, str(root))
    assert locate.find_data_root() == root


def test_scan_drives_finds_nested_root(tmp_path, monkeypatch):
    """模拟「文件管理」改到 D:\\Tencent\\Documents 的情况：三层之内必须扫到。"""
    nested = _make_root(tmp_path / "Tencent" / "Documents")
    monkeypatch.setattr(locate, "_fixed_drives", lambda: [str(tmp_path) + "\\"])
    found = locate._scan_drives()
    assert nested in found


def test_scan_skips_system_dirs(tmp_path, monkeypatch):
    deep = _make_root(tmp_path / "Windows" / "System32")
    monkeypatch.setattr(locate, "_fixed_drives", lambda: [str(tmp_path) + "\\"])
    assert deep not in locate._scan_drives()


def test_output_dir_avoids_wechat_tree(tmp_path, monkeypatch):
    root = _make_root(tmp_path / "xwechat_files")
    inner = root / "wxid_test0" / "msg" / "file" / "2026-11"
    inner.mkdir(parents=True)
    monkeypatch.setattr("sys.argv", [str(inner / "tool.exe")])
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    out = locate.default_output_dir()
    assert locate.ROOT_NAME not in [p.lower() for p in out.parts]
    assert out.is_dir()


def test_output_dir_next_to_exe(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", [str(tmp_path / "app" / "tool.exe")])
    (tmp_path / "app").mkdir()
    out = locate.default_output_dir()
    assert out == tmp_path / "app" / "导出结果"
    assert json.dumps(str(out))  # 只是确认路径可序列化，供缓存/日志使用


def test_normalize_root_from_parent(tmp_path):
    root = _make_root(tmp_path / "Documents")
    assert locate.normalize_root(tmp_path / "Documents") == root


def test_normalize_root_from_account_dir(tmp_path):
    root = _make_root(tmp_path / "Documents")
    assert locate.normalize_root(root / "wxid_test0") == root


def test_normalize_root_from_db_storage(tmp_path):
    root = _make_root(tmp_path / "Documents")
    assert locate.normalize_root(root / "wxid_test0" / "db_storage") == root


def test_normalize_root_keeps_valid_path(tmp_path):
    root = _make_root(tmp_path)
    assert locate.normalize_root(root) == root
