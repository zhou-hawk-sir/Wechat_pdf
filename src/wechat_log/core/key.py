"""微信 4.0 数据库密钥获取。

微信 4.2 及之后不再把明文密钥直接放进内存，而是把形如
``x'<64 位 hex 密钥><32 位 hex 盐>'`` 的字面量按 32 字节掩码逐字节异或后存放。
本模块从运行中的微信进程内存扫描这种被掩码的密钥字面量，得到
``(密钥, 盐)`` 集合，再按各数据库文件首 16 字节的盐匹配密钥。

掩码本身是版本相关的常量。为了不因微信小版本调整而失效，除了默认掩码，还会：

1. 支持用环境变量 ``WECHAT_LOG_KEY_MASK`` 指定（多个用逗号分隔）；
2. 兜底用「已知盐反查」——库文件前 16 字节就是盐，直接去找它的 hex 串，
   命中后取前面 64 个 hex 字符即密钥，与掩码无关。

同时保留手动输入 hex 密钥的能力（``x'<hex>'`` 或裸 64/96 位 hex）。
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import psutil

from . import decrypt

# 32 字节异或掩码，微信 4.2 用它掩码内存中的密钥字面量。
MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
# 解掩码后匹配 ``x'<64~192 位 hex>'`` 字面量。
_LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
# 未掩码场景（手动输入、以及向后兼容的明文扫描）也用它。
_KEY_PATTERN = re.compile(rb"x'([0-9a-fA-F]+)'")
# 反查时认定密钥的形态：64 个 hex 字符紧邻盐的 hex。
_HEX_RUN = re.compile(rb"([0-9a-fA-F]{64})")


def masks() -> list[bytes]:
    """所有可尝试的掩码，默认掩码排在最前。"""
    found = [MASK]
    env = os.environ.get("WECHAT_LOG_KEY_MASK", "")
    for chunk in env.split(","):
        try:
            value = bytes.fromhex(chunk.strip())
        except ValueError:
            continue
        if len(value) == 32 and value not in found:
            found.append(value)
    return found


def _prefix_of(mask: bytes) -> bytes:
    """掩码后的 ``x'`` 前缀。"""
    return bytes((ord("x") ^ mask[0], ord("'") ^ mask[1]))


def parse_key_candidate(text: str | bytes) -> bytes | None:
    """从形如 ``x'<hex>'`` 的字符串解析出 32 字节密钥。"""
    if isinstance(text, str):
        text = text.encode("ascii", "ignore")
    match = _KEY_PATTERN.search(text)
    if not match:
        return None
    hexstr = match.group(1)
    if len(hexstr) not in (64, 96):
        return None
    try:
        data = bytes.fromhex(hexstr.decode("ascii"))
    except ValueError:
        return None
    return data[:32]


def extract_candidates(data: bytes) -> list[bytes]:
    """从一段字节里提取所有候选密钥，去重并保持出现顺序。"""
    seen: set[bytes] = set()
    out: list[bytes] = []
    for match in _KEY_PATTERN.finditer(data):
        key = parse_key_candidate(match.group(0))
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def parse_hex_key(text: str) -> tuple[bytes, bytes | None]:
    """解析用户输入，返回 ``(密钥, 盐)``；盐在输入含 96 位 hex 时才有。"""
    text = text.strip()
    if re.fullmatch(r"[0-9a-fA-F]{96}", text):
        data = bytes.fromhex(text)
        return data[:32], data[32:48]
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return bytes.fromhex(text), None
    candidate = parse_key_candidate(text)
    if candidate:
        return candidate, None
    return b"", None


def find_weixin_process() -> psutil.Process | None:
    """返回微信主进程（命令行最短者），找不到返回 None。"""
    procs = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in ("weixin.exe", "wechat.exe"):
            procs.append(proc)

    if not procs:
        return None
    procs.sort(key=lambda p: len(" ".join(p.info.get("cmdline") or [])))
    return procs[0]


# --- Windows 进程内存读取（ctypes，需要管理员权限） ---

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _PROCESS_VM_READ = 0x0010
    _PROCESS_QUERY_INFORMATION = 0x0400
    _MEM_COMMIT = 0x1000
    _PAGE_NOACCESS = 0x01
    _PAGE_GUARD = 0x100

    class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("PartitionId", wintypes.WORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    _kernel32 = ctypes.windll.kernel32

    def _enable_debug_privilege() -> None:
        """尽力提升到可读取其他进程内存的权限。"""
        advapi32 = ctypes.windll.advapi32
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            _kernel32.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(token)
        ):
            return
        try:
            luid = wintypes.LARGE_INTEGER()
            if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
                return
            tp = wintypes.TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = 0x00000002  # SE_PRIVILEGE_ENABLED
            advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
        finally:
            _kernel32.CloseHandle(token)

    def _iter_readable_regions(process) -> Iterator[bytes]:
        """枚举进程内已提交的可读内存区域，逐块返回内容。"""
        pid = int(process.pid)
        handle = _kernel32.OpenProcess(_PROCESS_VM_READ | _PROCESS_QUERY_INFORMATION, False, pid)
        if not handle:
            return
        try:
            address = 0
            mbi = _MEMORY_BASIC_INFORMATION()
            mbi_size = ctypes.sizeof(mbi)
            while _kernel32.VirtualQueryEx(
                handle, ctypes.c_void_p(address), ctypes.byref(mbi), mbi_size
            ):
                # c_void_p 值为 0 时 ctypes 返回 None（64 位下首个区域的基址为 0）。
                base = int(mbi.BaseAddress or 0)
                region = int(mbi.RegionSize or 0)
                protect = int(mbi.Protect)
                if (
                    region
                    and mbi.State == _MEM_COMMIT
                    and protect & (_PAGE_NOACCESS | _PAGE_GUARD) == 0
                ):
                    buffer = ctypes.create_string_buffer(region)
                    read = ctypes.c_size_t()
                    if _kernel32.ReadProcessMemory(
                        handle,
                        ctypes.c_void_p(base),
                        buffer,
                        region,
                        ctypes.byref(read),
                    ):
                        yield buffer.raw[: read.value]
                address = base + region
                if address <= base:
                    break
        finally:
            _kernel32.CloseHandle(handle)

    def collect_key_blobs(
        process, mask_list: Sequence[bytes] | None = None
    ) -> list[tuple[bytes, bytes]]:
        """扫描进程内存，返回去重后的 ``(密钥, 盐)`` 列表。"""
        _enable_debug_privilege()
        seen: set[tuple[bytes, bytes]] = set()
        blobs: list[tuple[bytes, bytes]] = []
        for mask in mask_list or masks():
            prefix = _prefix_of(mask)
            for region in _iter_readable_regions(process):
                index = region.find(prefix)
                while index != -1:
                    segment = region[index : index + 128]
                    decoded = bytes(b ^ mask[j % 32] for j, b in enumerate(segment))
                    match = _LITERAL.search(decoded)
                    if match:
                        run = match.group(1)
                        if len(run) >= 96:
                            key = bytes.fromhex(run[:64].decode("ascii"))
                            salt = bytes.fromhex(run[64:96].decode("ascii"))
                            if (key, salt) not in seen:
                                seen.add((key, salt))
                                blobs.append((key, salt))
                    index = region.find(prefix, index + 1)
        return blobs

    def find_key_by_salt(
        process, salt: bytes, mask_list: Sequence[bytes] | None = None
    ) -> bytes | None:
        """用已知的库盐反查密钥，不依赖掩码常量。

        内存里的字面量是 ``<64 hex 密钥><32 hex 盐>``，盐的 hex 串紧跟在密钥
        后面，所以拿盐当锚点、向前取 64 个 hex 字符即可。返回值仍由调用方校验。
        """
        if not salt:
            return None
        _enable_debug_privilege()
        hex_salt = salt.hex().encode()
        needles: list[bytes] = [hex_salt]
        for mask in mask_list or masks():
            needles.append(bytes(b ^ mask[i % 32] for i, b in enumerate(hex_salt)))
        for region in _iter_readable_regions(process):
            for needle in needles:
                start = region.find(needle)
                while start != -1:
                    window = region[max(0, start - 80) : start]
                    match = _HEX_RUN.search(window)
                    if match:
                        return bytes.fromhex(match.group(1).decode("ascii"))
                    start = region.find(needle, start + 1)
        return None

else:

    def collect_key_blobs(process, mask_list=None):  # pragma: no cover
        return []

    def find_key_by_salt(process, salt, mask_list=None):  # pragma: no cover
        return None


def _read_salt(db_path: str | Path) -> bytes:
    try:
        with Path(db_path).open("rb") as f:
            return f.read(16)
    except OSError:
        return b""


class KeyResolver:
    """一次内存扫描，缓存 ``盐 -> 密钥`` 映射，按需为各库匹配密钥。

    也支持用 ``keys`` 传入手动密钥（无盐），此时退化为逐库校验。
    """

    def __init__(self, process=None, blobs=None, keys=None):
        if blobs is not None:
            self.process = process
            self._blobs = list(blobs)
            self._keys: list[bytes] = []
        elif keys is not None:
            self.process = process
            self._blobs = []
            self._keys = [k for k in keys if k]
        else:
            self.process = process if process is not None else find_weixin_process()
            self._blobs = collect_key_blobs(self.process) if self.process else []
            self._keys = []
        self._by_salt = {salt: key for key, salt in self._blobs}
        self._verified: dict[str, bytes] = {}
        self._recovered: set[bytes] = set()

    @property
    def available(self) -> bool:
        return bool(self._by_salt or self._keys)

    @property
    def salts(self) -> set[bytes]:
        return set(self._by_salt)

    def _recover(self, salt: bytes, db_path: Path) -> bytes | None:
        """主扫描没覆盖到该盐时，用盐反查补齐（同一个盐只查一次）。"""
        if self.process is None or not salt or salt in self._recovered:
            return None
        self._recovered.add(salt)
        key = find_key_by_salt(self.process, salt)
        if key is not None and decrypt.verify_key(key, db_path):
            self._by_salt[salt] = key
            return key
        return None

    def key_for(self, db_path: str | Path) -> bytes | None:
        cache_key = str(db_path)
        if cache_key in self._verified:
            return self._verified[cache_key]

        salt = _read_salt(db_path)
        # 优先按盐直接匹配。
        key = self._by_salt.get(salt)
        if key is not None and decrypt.verify_key(key, db_path):
            self._verified[cache_key] = key
            return key

        # 掩码常量失配时用盐反查
        recovered = self._recover(salt, Path(db_path))
        if recovered is not None:
            self._verified[cache_key] = recovered
            return recovered

        # 手动密钥（无盐）时逐库校验。
        for key in self._keys:
            if decrypt.verify_key(key, db_path):
                self._verified[cache_key] = key
                return key
        return None


def acquire_key(db_path: str | Path, process=None) -> bytes | None:
    """自动从微信进程内存扫描并校验密钥（便捷封装）。"""
    return KeyResolver(process).key_for(db_path)
