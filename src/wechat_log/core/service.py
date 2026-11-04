"""导出编排：把解密、读库、媒体、导出器串成一次导出任务。"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from wechat_log.export import get_exporter
from wechat_log.export.base import ExportContext

from . import database, decrypt, locate
from .key import KeyResolver
from .media import MediaResolver
from .models import Account, Contact, ExportReport, ExportResult, Message, Session

ProgressFn = Callable[[float, str], None]

# 读取消息、还原媒体占一次导出的多少比例，剩下的留给各格式生成文件
_MEDIA_SHARE = 0.7
# 进度回调节流：最多每 0.12 秒汇报一次，避免信号刷爆界面
_STEP_SECONDS = 0.12

# 只匹配消息库 message_0.db / message_1.db ...，排除 media_* / message_fts / biz_message_* 等。
_MESSAGE_DB = re.compile(r"^message_(\d+)\.db$")
# 媒体统一放在输出目录的这个子文件夹下，每个会话再分一层。
MEDIA_DIR_NAME = "媒体"


def _message_db_paths(account: Account) -> list[Path]:
    msg_dir = account.root / "db_storage" / "message"
    if not msg_dir.is_dir():
        return []
    return sorted(p for p in msg_dir.iterdir() if _MESSAGE_DB.match(p.name))


def _self_wxids(account: Account) -> set[str]:
    """账号可能的自身标识（目录名可能带哈希后缀）。"""
    full = account.wxid
    base = full.rsplit("_", 1)[0] if "_" in full else full
    return {full, base}


def _safe_folder(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name or "").strip().strip(".")
    return cleaned or "未命名会话"


def _unique_path(folder: Path, name: str, used: set[str]) -> Path:
    """在 ``folder`` 下取一个不重名的路径。"""
    candidate = Path(name).name or "未命名"
    if candidate not in used:
        used.add(candidate)
        return folder / candidate
    stem, suffix = Path(candidate).stem, Path(candidate).suffix
    index = 2
    while f"{stem}_{index}{suffix}" in used:
        index += 1
    final = f"{stem}_{index}{suffix}"
    used.add(final)
    return folder / final


def _scaled(progress: ProgressFn | None, low: float, high: float) -> ProgressFn | None:
    """把局部进度映射到 ``[low, high]`` 区间；-1（估不出剩余时间）原样透传。"""
    if progress is None:
        return None

    def report(value: float, text: str) -> None:
        if value < 0:
            progress(-1, text)
            return
        ratio = min(max(value, 0.0), 1.0)
        progress(low + (high - low) * ratio, text)

    return report


class SessionExporter:
    """负责单个账号的解密缓存与导出。"""

    def __init__(
        self,
        account: Account,
        resolver: KeyResolver,
        display_names: dict[str, str] | None = None,
    ):
        self.account = account
        self.resolver = resolver
        self._display_names = display_names or {}
        self._temp = tempfile.TemporaryDirectory()
        self._cache: dict[Path, Path] = {}
        self._media: MediaResolver | None = None
        self._used_names: dict[str, set[str]] = {}

    def close(self) -> None:
        if self._media is not None:
            self._media.close()
            self._media = None
        self._temp.cleanup()

    @property
    def media(self) -> MediaResolver:
        if self._media is None:
            self._media = MediaResolver(self.account, self.resolver)
        return self._media

    def _decrypt(self, db_path: Path) -> Path | None:
        if db_path in self._cache:
            return self._cache[db_path]
        key = self.resolver.key_for(db_path)
        if key is None:
            return None
        dst = Path(self._temp.name) / db_path.name
        decrypt.decrypt_file(key, db_path, dst)
        self._cache[db_path] = dst
        return dst

    def iter_messages(self, talker: str) -> Iterable[Message]:
        """遍历某会话在所有消息库中的消息。"""
        self_ids = _self_wxids(self.account)
        for db_path in _message_db_paths(self.account):
            decrypted = self._decrypt(db_path)
            if decrypted is None:
                continue
            conn = database.open_decrypted(decrypted)
            try:
                name2id = database.load_name2id(conn)
                for msg in database.iter_messages(conn, talker, name2id):
                    # is_sender 需在把 sender 换成昵称之前判定（此时仍是 wxid）。
                    msg.is_sender = msg.sender in self_ids
                    if self._display_names:
                        msg.sender = self._display_names.get(msg.sender, msg.sender)
                    yield msg
            finally:
                conn.close()

    def count_messages(self, talker: str) -> int:
        """某会话的消息总条数，用来给进度条算分母。"""
        total = 0
        for db_path in _message_db_paths(self.account):
            decrypted = self._decrypt(db_path)
            if decrypted is None:
                continue
            conn = database.open_decrypted(decrypted)
            try:
                total += database.count_messages(conn, talker)
            finally:
                conn.close()
        return total

    def export_one(
        self,
        session: Session,
        display_name: str,
        formats: list[str],
        out_dir: Path,
        copy_media: bool = True,
        per_session_folder: bool = False,
        progress: ProgressFn | None = None,
    ) -> list[ExportResult]:
        """导出单个会话到所有选定格式。"""
        target_dir = out_dir / _safe_folder(display_name) if per_session_folder else out_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        exporters = [e for e in (get_exporter(fmt) for fmt in formats) if e is not None]
        if progress is not None:
            progress(-1, f"正在统计：{display_name} 的消息数量 …")
        total = self.count_messages(session.user_name)

        def on_step(done: int) -> None:
            if progress is None:
                return
            if total:
                progress(min(done / total, 1.0) * _MEDIA_SHARE, f"正在读取：{display_name}（{done}/{total} 条）")
            else:
                progress(-1, f"正在读取：{display_name}（{done} 条）")

        messages = self._materialize(
            session.user_name, display_name, target_dir, copy_media, on_step
        )
        results = []
        for index, exporter in enumerate(exporters):
            low = _MEDIA_SHARE + (1 - _MEDIA_SHARE) * index / len(exporters)
            high = _MEDIA_SHARE + (1 - _MEDIA_SHARE) * (index + 1) / len(exporters)
            ctx = ExportContext(
                session=session,
                display_name=display_name,
                messages=messages,
                out_dir=target_dir,
                progress=_scaled(progress, low, high),
            )
            results.append(exporter.export(ctx))
        if progress is not None and not exporters:
            progress(1.0, f"已跳过：{display_name}")
        return results

    def _materialize(
        self,
        talker: str,
        display_name: str,
        out_dir: Path,
        copy_media: bool,
        on_step: Callable[[int], None] | None = None,
    ) -> list[Message]:
        messages: list[Message] = []
        folder = out_dir / MEDIA_DIR_NAME / _safe_folder(display_name)
        used = self._used_names.setdefault(str(folder), set())
        last = 0.0
        for count, msg in enumerate(self.iter_messages(talker), start=1):
            if copy_media and msg.media is None:
                try:
                    msg.media = self.media.resolve(msg)
                except Exception:  # noqa: BLE001 - 单条媒体失败不应中断导出
                    msg.media = None
            if msg.media is not None and msg.media.source is not None:
                rel = self._save_media(msg.media, folder, used, out_dir)
                if rel is not None:
                    msg.media.source = rel
                else:
                    msg.media = None
            messages.append(msg)
            if on_step is not None:
                now = time.monotonic()
                if now - last >= _STEP_SECONDS:
                    last = now
                    on_step(count)
        if on_step is not None:
            on_step(len(messages))
        return messages

    def _save_media(self, media, folder: Path, used: set[str], out_dir: Path) -> Path | None:
        """把还原好的媒体拷进输出目录，返回相对路径。"""
        src = Path(media.source)
        if not src.exists():
            return None
        folder.mkdir(parents=True, exist_ok=True)
        dst = _unique_path(folder, media.name or src.name, used)
        try:
            # 用 copyfile 而非 copy2：不继承源文件的只读属性，否则重复导出会失败
            shutil.copyfile(src, dst)
            dst.chmod(0o666)
        except OSError:
            return None
        return dst.relative_to(out_dir)


def _decrypt_to_temp(
    account: Account, resolver: KeyResolver, rel_path: str
) -> tuple[Path, Path] | None:
    """解密某个库到临时目录，返回 (临时文件, 临时目录)。"""
    db_path = account.root / "db_storage" / rel_path
    if not db_path.exists():
        return None
    key = resolver.key_for(db_path)
    if key is None:
        return None
    temp_dir = Path(tempfile.mkdtemp(prefix="wechat_log_"))
    dst = temp_dir / db_path.name
    decrypt.decrypt_file(key, db_path, dst)
    return dst, temp_dir


def load_contacts(account: Account, resolver: KeyResolver) -> list[Contact]:
    """解密并读取联系人。"""
    result = _decrypt_to_temp(account, resolver, "contact/contact.db")
    if result is None:
        return []
    dst, temp_dir = result
    try:
        conn = database.open_decrypted(dst)
        try:
            return database.load_contacts(conn)
        finally:
            conn.close()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def load_sessions(account: Account, resolver: KeyResolver) -> list[Session]:
    """解密并读取会话列表。"""
    result = _decrypt_to_temp(account, resolver, "session/session.db")
    if result is None:
        return []
    dst, temp_dir = result
    try:
        conn = database.open_decrypted(dst)
        try:
            return database.load_sessions(conn)
        finally:
            conn.close()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def detect_active_account(data_root: Path, resolver: KeyResolver) -> Account | None:
    """返回密钥能解密的账号（即当前登录账号），找不到返回 None。"""
    for account in locate.list_accounts(data_root):
        contact_db = account.root / "db_storage" / "contact" / "contact.db"
        if contact_db.exists() and resolver.key_for(contact_db) is not None:
            return account
    return None


def export_sessions(
    account: Account,
    resolver: KeyResolver,
    sessions: list[tuple[Session, str]],
    formats: list[str],
    out_dir: Path,
    copy_media: bool = True,
    per_session_folder: bool = False,
    progress: ProgressFn | None = None,
) -> ExportReport:
    """批量导出多个会话，返回汇总报告。"""
    if progress:
        progress(-1, "正在解密数据库、读取联系人 …")
    contacts = load_contacts(account, resolver)
    display_names = {c.user_name: c.display_name for c in contacts}
    exporter = SessionExporter(account, resolver, display_names)
    report = ExportReport()
    try:
        total = max(len(sessions), 1)
        for index, (session, display_name) in enumerate(sessions):
            low = index / total
            high = (index + 1) / total
            results = exporter.export_one(
                session,
                display_name,
                formats,
                out_dir,
                copy_media=copy_media,
                per_session_folder=per_session_folder,
                progress=_scaled(progress, low, high),
            )
            report.results.extend(results)
    finally:
        exporter.close()
    if progress:
        progress(1.0, "导出完成")
    return report
