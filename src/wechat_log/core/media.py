"""媒体附件解析：图片、语音、视频、文件。

微信 4.x 的媒体分散在几处：

* ``db_storage/hardlink/hardlink.db`` 把消息里的 md5 映射到磁盘文件名与目录，
  图片、文件、视频各有一张表，``dir2id`` 把目录编号还原成真实目录名。
* 图片落在 ``msg/attach/<dir1>/<dir2>/Img/<file>.dat``，``.dat`` 由
  :mod:`wechat_log.core.imagekey` 解封装。
* 语音存在 ``db_storage/message/media_*.db`` 的 ``VoiceInfo`` 表，SILK 编码，
  用 ``pilk`` 转成 WAV。
* 视频与文件基本是明文，按 hardlink 映射直接定位。

``hardlink.db`` 索引不全时，图片还可以按「文件修改时间最接近消息时间」兜底
匹配——同一条消息只占用一个文件，避免重复引用。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import content, decrypt
from .imagekey import ImageDecoder, resolve_image_key, sniff_image
from .models import Account, Media, Message

try:  # 语音转码是可选能力，缺依赖时保留 SILK 原文件
    import pilk
except ImportError:  # pragma: no cover - 取决于运行环境
    pilk = None

_MD5 = re.compile(r'md5="([0-9a-fA-F]{32})"')
_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_FILEEXT = re.compile(r"<fileext>([^<]*)</fileext>", re.S)

VOICE_RATE = 24000

# 窗口版程序没有控制台，直接起子进程会弹黑框；带上这个标志让子进程静默运行
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _timestamp_name(ts: int, index: int, ext: str) -> str:
    """用消息时间生成稳定、可读的文件名。"""
    try:
        stamp = datetime.fromtimestamp(ts).strftime("%Y%m%d_%H%M%S")
    except (OverflowError, OSError, ValueError):
        stamp = str(ts)
    return f"{stamp}_{index:02d}{ext}"


def find_ffmpeg() -> str | None:
    """定位 ffmpeg（可选，仅用于 HEVC 动图取首帧）。"""
    import sys

    exe_dir = Path(sys.executable).parent
    for path in (exe_dir / "ffmpeg.exe", Path(sys.argv[0]).parent / "ffmpeg.exe"):
        if path.exists():
            return str(path)
    found = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
    if found:
        return found
    for base in (Path("C:/"), Path("D:/"), Path("E:/")):
        for pattern in ("ffmpeg/*/bin/ffmpeg.exe", "ffmpeg/bin/ffmpeg.exe"):
            for path in base.glob(pattern):
                if path.exists():
                    return str(path)
    return None


def hevc_first_frame(data: bytes, ffmpeg: str | None = None) -> bytes | None:
    """把 ``wxgf``（HEVC 裸流）解成首帧 PNG。"""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return None
    start = data.find(b"\x00\x00\x00\x01")
    if start < 0:
        start = data.find(b"\x00\x00\x01")
    if start < 0:
        return None
    with tempfile.TemporaryDirectory(prefix="wechat_log_hevc_") as tmp:
        src = Path(tmp) / "in.hevc"
        dst = Path(tmp) / "out.png"
        src.write_bytes(data[start:])
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "hevc",
                    "-i",
                    str(src),
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    str(dst),
                ],
                capture_output=True,
                timeout=90,
                stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if dst.exists() and dst.stat().st_size > 0:
            return dst.read_bytes()
    return None


class MediaResolver:
    """为一个账号提供 :meth:`resolve`，把消息转成可落盘的文件。

    解密得到的索引库与还原出的媒体都放在临时目录里，``close()`` 时清理。
    """

    def __init__(self, account: Account, key_resolver):
        self.account = account
        self._keys = key_resolver
        self._tmp = Path(tempfile.mkdtemp(prefix="wechat_log_media_"))
        self._counter = 0

        self._dir2id: dict[int, str] = {}
        self._images: dict[str, tuple[str, int, int]] = {}
        self._files: dict[str, tuple[str, int, int]] = {}
        self._videos: dict[str, tuple[str, int, int]] = {}
        self._voices: dict[tuple[str, int], bytes] = {}

        self._decoder: ImageDecoder | None = None
        self._decoder_tried = False
        self._stems: dict[str, list[tuple[int, str, int, Path]]] = {}
        self._used: set[str] = set()

        self._load_hardlink()
        self._load_voices()

    # ------------------------------------------------------------------ 索引
    def _decrypt_index(self, rel: str) -> Path | None:
        src = self.account.root / "db_storage" / rel
        if not src.exists():
            return None
        key = self._keys.key_for(src)
        if key is None:
            return None
        dst = self._tmp / f"idx_{src.parent.name}_{src.name}"
        try:
            decrypt.decrypt_file(key, src, dst)
        except OSError:
            return None
        return dst

    def _load_hardlink(self) -> None:
        path = self._decrypt_index("hardlink/hardlink.db")
        if path is None:
            return
        try:
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            return
        try:
            try:
                self._dir2id = {
                    int(row[0]): str(row[1])
                    for row in conn.execute("SELECT rowid, username FROM dir2id")
                }
            except sqlite3.Error:
                self._dir2id = {}
            for table, target in (
                ("image_hardlink_info_v4", self._images),
                ("file_hardlink_info_v4", self._files),
                ("video_hardlink_info_v4", self._videos),
            ):
                try:
                    rows = conn.execute(f"SELECT md5, file_name, dir1, dir2 FROM {table}")
                except sqlite3.Error:
                    continue
                for md5, name, d1, d2 in rows:
                    if md5:
                        target[str(md5).lower()] = (str(name), int(d1 or 0), int(d2 or 0))
        finally:
            conn.close()

    def _load_voices(self) -> None:
        folder = self.account.root / "db_storage" / "message"
        if not folder.is_dir():
            return
        for src in sorted(folder.glob("media_*.db")):
            path = self._decrypt_index(f"message/{src.name}")
            if path is None:
                continue
            try:
                conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            except sqlite3.Error:
                continue
            try:
                try:
                    names = {
                        int(row[0]): str(row[1])
                        for row in conn.execute("SELECT rowid, user_name FROM Name2Id")
                    }
                except sqlite3.Error:
                    names = {}
                try:
                    rows = list(
                        conn.execute("SELECT chat_name_id, create_time, voice_data FROM VoiceInfo")
                    )
                except sqlite3.Error:
                    rows = []
            finally:
                conn.close()
            for chat_id, created, data in rows:
                user = names.get(int(chat_id or 0))
                if not user or not data:
                    continue
                self._voices[(user, int(created or 0))] = bytes(data)

    # ---------------------------------------------------------------- 路径
    def _path_of(self, name: str, d1: int, d2: int, kind: str) -> Path | None:
        root = self.account.root / "msg"
        if kind == "image":
            # 图片：attach/<dir2id[dir1]>/<dir2id[dir2]>/Img/<file>
            a, b = self._dir2id.get(d1), self._dir2id.get(d2)
            if not (a and b):
                return None
            return root / "attach" / a / b / "Img" / name
        # 视频与文件只用到 dir1（月份目录），dir2 通常是 0。
        folder = self._dir2id.get(d1) or self._dir2id.get(d2)
        if not folder:
            return None
        return root / ("video" if kind == "video" else "file") / folder / name

    def _decoder_or_none(self) -> ImageDecoder | None:
        if not self._decoder_tried:
            self._decoder_tried = True
            key = resolve_image_key(self.account.root, self.account.wxid)
            if key is not None:
                self._decoder = ImageDecoder(key)
        return self._decoder

    # -------------------------------------------------- 按修改时间兜底匹配
    def _talker_stems(self, talker: str) -> list[tuple[int, str, int, Path]]:
        if talker in self._stems:
            return self._stems[talker]
        root = self.account.root / "msg" / "attach" / hashlib.md5(talker.encode()).hexdigest()
        groups: dict[str, list] = {}
        if root.is_dir():
            for path in root.rglob("*.dat"):
                stem, rank = path.stem, 0
                for suffix, value in (("_t_W", 3), ("_t", 2), ("_h", 1)):
                    if stem.endswith(suffix):
                        stem, rank = stem[: -len(suffix)], value
                        break
                try:
                    mtime = int(path.stat().st_mtime)
                except OSError:
                    continue
                current = groups.get(stem)
                if current is None:
                    groups[stem] = [mtime, rank, path]
                else:
                    current[0] = min(current[0], mtime)
                    if rank < current[1]:
                        current[1], current[2] = rank, path
        items = sorted((v[0], k, v[1], v[2]) for k, v in groups.items())
        self._stems[talker] = items
        return items

    def _match_by_time(self, talker: str, ts: int, tol: int = 86400) -> Path | None:
        items = self._talker_stems(talker)
        best: tuple[str, Path] | None = None
        distance = 10**9
        for mtime, stem, _rank, path in items:
            if stem in self._used:
                continue
            delta = abs(mtime - int(ts))
            if delta < distance:
                distance, best = delta, (stem, path)
        if best and distance <= tol:
            self._used.add(best[0])
            return best[1]
        return None

    # ---------------------------------------------------------------- 落盘
    def _emit(self, data: bytes, name: str, kind: str) -> Media | None:
        if not data:
            return None
        dst = self._tmp / name
        try:
            dst.write_bytes(data)
        except OSError:
            return None
        return Media(kind=kind, source=dst, name=name)

    def _decode_image_file(self, path: Path) -> tuple[bytes, str] | None:
        decoder = self._decoder_or_none()
        if decoder is None:
            return None
        data = decoder.decrypt_file(path)
        if not data:
            return None
        ext, known = sniff_image(data)
        if known:
            return data, ext
        if ext == ".wxgf":
            png = hevc_first_frame(data)
            if png:
                return png, ".png"
        return None

    # ---------------------------------------------------------------- 对外
    def resolve(self, message: Message) -> Media | None:
        """为一条消息还原媒体文件；无法还原返回 ``None``。"""
        kind = message.kind
        self._counter += 1
        index = self._counter
        if kind == "image":
            return self._image(message, index)
        if kind == "voice":
            return self._voice(message, index)
        if kind == "video":
            return self._video(message, index)
        if kind == "app":
            return self._attachment(message)
        return None

    def _image(self, message: Message, index: int) -> Media | None:
        xml = content.message_xml(message.raw_content) or ""
        candidates: list[Path] = []
        entry = next(
            (
                self._images[m.group(1).lower()]
                for m in _MD5.finditer(xml)
                if m.group(1).lower() in self._images
            ),
            None,
        )
        if entry:
            name, d1, d2 = entry
            full = self._path_of(name, d1, d2, "image")
            if full is not None:
                for cand in (full, full.with_name(f"{Path(name).stem}_t.dat")):
                    if cand.exists():
                        candidates.append(cand)
        fallback = self._match_by_time(message.talker, message.create_time)
        if fallback is not None:
            candidates.append(fallback)

        for path in candidates:
            decoded = self._decode_image_file(path)
            if decoded is not None:
                data, ext = decoded
                return self._emit(data, _timestamp_name(message.create_time, index, ext), "image")
        return None

    def _voice(self, message: Message, index: int) -> Media | None:
        silk = self._voices.get((message.talker, int(message.create_time)))
        if not silk:
            return None
        raw_name = _timestamp_name(message.create_time, index, ".silk")
        if pilk is None:
            return self._emit(silk, raw_name, "voice")
        with tempfile.TemporaryDirectory(prefix="wechat_log_silk_") as tmp:
            src = Path(tmp) / "v.silk"
            dst = Path(tmp) / "v.wav"
            src.write_bytes(silk)
            try:
                pilk.silk_to_wav(str(src), str(dst), VOICE_RATE)
            except Exception:  # noqa: BLE001 - 转码失败要能降级
                return self._emit(silk, raw_name, "voice")
            if not dst.exists() or dst.stat().st_size <= 44:
                return self._emit(silk, raw_name, "voice")
            return self._emit(
                dst.read_bytes(),
                _timestamp_name(message.create_time, index, ".wav"),
                "voice",
            )

    def _video(self, message: Message, index: int) -> Media | None:
        xml = content.message_xml(message.raw_content) or ""
        for match in _MD5.finditer(xml):
            entry = self._videos.get(match.group(1).lower())
            if entry is None:
                continue
            name, d1, d2 = entry
            path = self._path_of(name, d1, d2, "video")
            if path is not None and path.exists():
                try:
                    return self._emit(path.read_bytes(), Path(name).name, "video")
                except OSError:
                    continue
        return None

    def _attachment(self, message: Message) -> Media | None:
        xml = content.message_xml(message.raw_content)
        if not xml:
            return None
        # 优先用 hardlink 的 md5 映射，文件名即原始文件名
        for match in _MD5.finditer(xml):
            entry = self._files.get(match.group(1).lower())
            if entry is None:
                continue
            name, d1, d2 = entry
            path = self._path_of(name, d1, d2, "file")
            if path is not None and path.exists():
                try:
                    return self._emit(path.read_bytes(), Path(name).name, "file")
                except OSError:
                    continue

        ext = _FILEEXT.search(xml)
        title = _TITLE.search(xml)
        name = title.group(1).strip() if title else ""
        if not name:
            return None
        has_ext = bool(ext and ext.group(1)) or bool(re.search(r"\.[A-Za-z0-9]{1,8}$", name))
        if not has_ext:
            return None
        safe = Path(name).name
        base = self.account.root / "msg" / "file"
        if not safe or not base.is_dir():
            return None
        for path in base.rglob(safe):
            if path.is_file():
                try:
                    return self._emit(path.read_bytes(), safe, "file")
                except OSError:
                    return None
        return None

    def close(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
