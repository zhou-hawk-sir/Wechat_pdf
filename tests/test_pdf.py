"""PDF 元数据清理与浏览器定位的测试。"""

from wechat_log.export.pdf import (
    _blank_info_string,
    _candidates,
    _is_elevated,
    _log_hint,
    _time_budget,
    html_to_pdf,
    strip_pdf_metadata,
)


def _blob() -> bytearray:
    return bytearray(
        b"<< /Title (chat) /Creator (Mozilla/5.0 \\(Windows NT 10.0\\)) "
        b"/Producer (Skia/PDF m156) >>"
    )


def test_blank_creator_and_producer():
    data = _blob()
    length = len(data)
    assert _blank_info_string(data, b"Creator")
    assert _blank_info_string(data, b"Producer")
    # 长度不变，xref 偏移才不会错位
    assert len(data) == length
    assert b"Mozilla" not in data
    assert b"Skia/PDF" not in data
    assert b"/Title (chat)" in data


def test_blank_missing_key_is_noop():
    data = _blob()
    assert not _blank_info_string(data, b"Author")
    assert data == _blob()


def test_strip_on_real_file(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(bytes(_blob()))
    strip_pdf_metadata(path)
    assert b"Mozilla" not in path.read_bytes()


def test_strip_missing_file_is_silent(tmp_path):
    strip_pdf_metadata(tmp_path / "nope.pdf")


def test_candidates_are_unique():
    """ProgramFiles 与 ProgramW6432 常指向同一目录，候选不能重复。"""
    paths = _candidates()
    assert len(paths) == len(set(paths))


def test_candidates_prefer_chrome():
    """旧版 Edge 不认 --headless=new，有 Chrome 时排前面。"""
    paths = _candidates()
    names = [p.rsplit("\\", 1)[-1].lower() for p in paths]
    if "chrome.exe" in names and "msedge.exe" in names:
        assert names.index("chrome.exe") < names.index("msedge.exe")


def test_time_budget_grows_with_images(tmp_path):
    """图片越多给浏览器的渲染预算越长，但有上限。"""
    plain = tmp_path / "a.html"
    plain.write_text("<div>no image</div>", encoding="utf-8")
    heavy = tmp_path / "b.html"
    heavy.write_text("<img src='x'>" * 200, encoding="utf-8")
    assert _time_budget(plain) == 8000
    assert _time_budget(heavy) > _time_budget(plain)
    assert _time_budget(heavy) <= 60000


def test_time_budget_on_missing_file(tmp_path):
    assert _time_budget(tmp_path / "nope.html") == 8000


def test_is_elevated_returns_bool():
    """提权检测只用于补错误提示，取不到值时必须当作普通权限。"""
    assert isinstance(_is_elevated(), bool)


def test_log_hint_skips_gpu_noise(tmp_path):
    """正常打印也会写 GPU 警告，不能把它当成失败原因。"""
    log = tmp_path / "chrome_debug.log"
    log.write_text(
        "[1:2:1102/000000.000:ERROR:ui\\gl\\direct_composition_support.cc:255] "
        "GetGpuDriverOverlayInfo: Failed to retrieve video device\n"
        "[3:4:1102/000000.001:ERROR:headless\\headless_printing.cc:80] Printing failed.\n",
        encoding="utf-8",
    )
    hint = _log_hint(str(tmp_path))
    assert "Printing failed" in hint
    assert "GetGpuDriverOverlayInfo" not in hint


def test_log_hint_without_log(tmp_path):
    assert _log_hint(str(tmp_path / "nope")) == ""


def test_html_to_pdf_without_browser_raises(tmp_path):
    html = tmp_path / "a.html"
    html.write_text("<div>x</div>", encoding="utf-8")
    try:
        html_to_pdf(html, tmp_path / "a.pdf", [])
    except RuntimeError as exc:
        assert "未找到" in str(exc)
    else:
        raise AssertionError("没有浏览器时应当抛错")
