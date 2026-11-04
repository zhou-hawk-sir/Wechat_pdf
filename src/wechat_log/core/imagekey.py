"""聊天图片密钥推导与 ``.dat`` 解密。

图片在磁盘上以 ``.dat`` 封装，密钥不是随机的，而是由本机登录状态派生：

* ``aes_key = md5(str(uin) + wxid)[:16]``，作为 16 字节 ASCII 使用
* ``xor_key = uin & 0xFF``，用于尾段单字节异或

``uin`` 可以从 MMKV 的 ``key_<uin>_..._input.statistic`` 文件名里读到，wxid 则是
账号目录名。两边都有多个可能取值，所以这里枚举组合，用真实 ``.dat`` 校验。

封装格式：:

    V2     : [6B 魔数 07 08 56 32 08 07][aes_len u32le][xor_len u32le][1B pad]
             [AES-128-ECB 密文][16B 间隔][单字节 XOR 尾段]
    legacy : 整文件单字节 XOR
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from Crypto.Cipher import AES

V2_MAGIC = b"\x07\x08V2\x08\x07"
# 数据目录内那份密钥缓存用的键，与路径无关，换机器后仍能对上
_ACCOUNT_KEY = "account"

# 解密后的常见图片头 -> 扩展名。顺序有意义，RIFF 必须晚于其它判断。
_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"wxgf", ".wxgf"),
    (b"\x89PNG", ".png"),
    (b"GIF8", ".gif"),
    (b"RIFF", ".webp"),
    (b"BM", ".bmp"),
)


def sniff_image(data: bytes) -> tuple[str, bool]:
    """识别解密后的数据格式，返回 ``(扩展名, 是否已知格式)``。"""
    if not data:
        return "", False
    for magic, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return ext, ext != ".wxgf"
    return "", False


@dataclass
class ImageKey:
    """一套可用的图片解密参数。"""

    uin: int
    wxid: str
    aes_key: bytes

    @property
    def xor_key(self) -> int:
        return self.uin & 0xFF


class ImageDecoder:
    """按 :class:`ImageKey` 解密单个 ``.dat``。"""

    def __init__(self, key: ImageKey):
        self.key = key

    def decrypt_bytes(self, data: bytes) -> bytes:
        if data[: len(V2_MAGIC)] == V2_MAGIC:
            aes_len, xor_len = struct.unpack("<II", data[6:14])
            head = data[15 : 15 + aes_len]
            if len(head) < aes_len:
                return b""
            plain = AES.new(self.key.aes_key, AES.MODE_ECB).decrypt(head)
            start = 15 + aes_len + 16
            tail = data[start : start + xor_len]
            return plain + bytes(b ^ self.key.xor_key for b in tail)
        # legacy：整文件单字节异或
        return bytes(b ^ self.key.xor_key for b in data)

    def decrypt_file(self, path: str | Path) -> bytes:
        try:
            with Path(path).open("rb") as fh:
                return self.decrypt_bytes(fh.read())
        except OSError:
            return b""


def uin_candidates() -> list[int]:
    """从 MMKV 统计文件名里收集所有可能的 uin。

    微信不把 uin 写进 ``xwechat_files``，只出现在它自己的程序数据目录
    （``%APPDATA%\\Tencent\\xwechat``）的文件名里。所以这里也接受用户用
    ``WECHAT_LOG_UIN`` 指定的值，供只带了数据、没有程序目录的场景使用。
    """
    found: list[int] = []
    appdata = os.environ.get("APPDATA", "")
    base = Path(appdata) / "Tencent" if appdata else None
    patterns = []
    if base is not None:
        for sub in ("xwechat", "WeChat"):
            root = base / sub
            if not root.is_dir():
                continue
            patterns += [
                root.glob("*/kvcomm/*.statistic"),
                root.glob("*/*/kvcomm/*.statistic"),
                root.glob("*/kvcomm/**/*.statistic"),
            ]
    for group in patterns:
        for path in group:
            for digits in re.findall(r"\d{4,}", path.name):
                value = int(digits)
                if value not in found:
                    found.append(value)
    # 位数多的更像真正的 uin，优先尝试
    found.sort(key=lambda v: len(str(v)), reverse=True)

    env_uin = os.environ.get("WECHAT_LOG_UIN", "").strip()
    if env_uin.isdigit():
        value = int(env_uin)
        # 显式指定的排最前
        found = [value] + [v for v in found if v != value]
    return found


def wxid_candidates(own_wxid: str, account_dir: str | Path | None = None) -> list[str]:
    """账号名的可能形态（目录名可能带 4 位哈希后缀）。"""
    out: list[str] = []
    for value in (own_wxid, Path(account_dir).name if account_dir else ""):
        if not value:
            continue
        if value not in out:
            out.append(value)
        match = re.match(r"^(wxid_[0-9a-zA-Z]+)_[0-9a-f]{4}$", value)
        if match and match.group(1) not in out:
            out.append(match.group(1))
    return out


def _key_of(uin: int, wxid: str) -> bytes:
    return hashlib.md5(f"{uin}{wxid}".encode()).hexdigest()[:16].encode()


def _sample_dat(account_dir: Path) -> Path | None:
    """挑一个体积适中的 V2 ``.dat`` 作为校验样本。"""
    root = account_dir / "msg" / "attach"
    if not root.is_dir():
        return None
    for path in root.rglob("*.dat"):
        try:
            if path.stat().st_size < 4096:
                continue
            with path.open("rb") as fh:
                if fh.read(len(V2_MAGIC)) != V2_MAGIC:
                    continue
        except OSError:
            continue
        return path
    return None


def _looks_decrypted(data: bytes) -> bool:
    ext, known = sniff_image(data)
    return known or ext == ".wxgf"


def _cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(base) / "wechat_log" / "image_key.json"


def _cache_paths(account_dir: Path) -> list[Path]:
    """缓存放两处。

    数据目录里那份是为了跟着数据走：uin 只存在于微信自己的程序数据里
    （``%APPDATA%\\Tencent\\xwechat``），把 ``xwechat_files`` 拷到别的机器时
    那份程序数据往往不会跟过去，没有 uin 就解不出图片。在数据目录里留一份
    记住的密钥，换机器后直接读，不必依赖 uin。
    """
    return [account_dir / ".wechat_log_image_key.json", _cache_path()]


def resolve_image_key(
    account_dir: str | Path,
    own_wxid: str,
    use_cache: bool = True,
) -> ImageKey | None:
    """推导该账号的图片密钥；无法推导时返回 ``None``。"""
    account_dir = Path(account_dir)
    cached = _read_cache(account_dir, own_wxid) if use_cache else None
    if cached is not None:
        return cached

    sample = _sample_dat(account_dir)
    if sample is None:
        return None
    try:
        with sample.open("rb") as fh:
            raw = fh.read()
    except OSError:
        return None

    for uin in uin_candidates():
        for wxid in wxid_candidates(own_wxid, account_dir):
            key = ImageKey(uin=uin, wxid=wxid, aes_key=_key_of(uin, wxid))
            if _looks_decrypted(ImageDecoder(key).decrypt_bytes(raw)):
                if use_cache:
                    _write_cache(account_dir, own_wxid, key)
                return key
    return None


def _read_cache(account_dir: Path, own_wxid: str) -> ImageKey | None:
    for path in _cache_paths(account_dir):
        key = _load_cache_entry(path, account_dir, own_wxid)
        if key is not None:
            return key
    return None


def _load_cache_entry(path: Path, account_dir: Path, own_wxid: str) -> ImageKey | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 数据目录里那份用固定键，换机器后路径变了也认
    entry = data.get(str(account_dir)) or data.get(_ACCOUNT_KEY)
    if not entry or entry.get("wxid_source") != own_wxid:
        return None
    try:
        return ImageKey(
            uin=int(entry["uin"]),
            wxid=str(entry["wxid"]),
            aes_key=bytes.fromhex(entry["aes_key"]),
        )
    except (KeyError, ValueError):
        return None


def _write_cache(account_dir: Path, own_wxid: str, key: ImageKey) -> None:
    entry = {
        "uin": key.uin,
        "wxid": key.wxid,
        "wxid_source": own_wxid,
        "aes_key": key.aes_key.hex(),
    }
    _write_entry(account_dir / ".wechat_log_image_key.json", {_ACCOUNT_KEY: entry})

    local = _cache_path()
    try:
        data = json.loads(local.read_text(encoding="utf-8")) if local.exists() else {}
    except (OSError, ValueError):
        data = {}
    data[str(account_dir)] = entry
    _write_entry(local, data)


def _write_entry(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # 数据目录只读（比如从备份盘挂载）时忽略，不影响其余流程
        pass
