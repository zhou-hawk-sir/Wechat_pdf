"""主窗口：三步式单窗口界面。

顶部是状态与提示，往下一排带序号的按钮，再是导出选项、会话列表和日志。
取密钥不需要重启微信，开着就能扫描内存，所以整个流程就是
「自动完成 → 选会话 → 导出」。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wechat_log.core import locate, service
from wechat_log.core.key import KeyResolver, parse_hex_key
from wechat_log.core.models import Session

FORMATS = [("pdf", "PDF"), ("html", "HTML"), ("txt", "TXT"), ("csv", "CSV"), ("json", "JSON")]

HINTS = {
    "scan": "正在读取微信数据，请稍候 …",
    "ready": "① 在列表里选中会话（Ctrl / Shift 可多选） ② 勾选格式 ③ 点右下角导出",
    "no_session": "列表里没有会话：确认微信已登录并保持运行，然后点「读取会话列表」",
    "exporting": "正在导出，请稍候 …（会话消息多、勾了 PDF 时会慢一些）",
    "done": "导出完成，可直接打开输出目录查看",
}

HELP_TEXT = """【三步就够】

① 获取密钥 / 读取会话列表
   · 微信保持登录并运行，点一下即可，通常几秒完成
   · 本工具从微信进程内存里读出数据库密钥，不需要重启微信
   · 读完后列表里就是全部会话，搜索框可按昵称、备注、wxid 过滤

② 选会话、选格式
   · 列表支持 Ctrl / Shift 多选，也可以「全选」
   · 格式可多选，也可只选一种：
       PDF  —— 气泡排版，适合存档、打印
       HTML —— 同款排版，语音可直接点开播放
       TXT  —— 纯文本，方便搜索复制
       CSV  —— 表格，可用 Excel 打开
       JSON —— 结构化数据，方便二次处理
   · 勾「还原图片和语音」会把图片解密成 jpg/png、语音转成 wav，
     一起放进输出目录的「媒体\\会话名」里；不勾则只留 [图片] / [语音] 占位

③ 导出选中会话
   · 单条会话出错不会中断其他会话，最后会给出汇总

【常见问题】
   · 取不到密钥：确认微信已登录并保持运行，且本工具不是以管理员身份启动
   · 找不到数据：点「数据目录」手动指定 xwechat_files 所在位置
   · 少图片/语音：微信按需下载，本机没存过的媒体无法还原；
     可在微信里打开该会话向上滚动加载后再导出
   · PDF 需要本机装有 Chrome 或 Edge（一般都有）
"""


class PrepareWorker(QThread):
    """后台完成：定位目录 → 取密钥 → 识别账号 → 读会话列表。"""

    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, data_root: Path | None = None, manual_key: bytes | None = None):
        super().__init__()
        self.data_root = data_root
        self.manual_key = manual_key

    def run(self):  # noqa: D102 - QThread 约定
        try:
            root = self.data_root or locate.find_data_root()
            if root is None:
                self.failed.emit("没有找到微信数据目录，请点「数据目录」手动指定")
                return
            resolver = KeyResolver(keys=[self.manual_key]) if self.manual_key else KeyResolver()
            if not resolver.available:
                self.failed.emit(
                    "未能获取密钥：请确认微信已登录并保持运行，"
                    "且本工具未以管理员身份启动（提权会让读取失败）"
                )
                return
            account = service.detect_active_account(root, resolver)
            if account is None:
                self.failed.emit("密钥无法解开任何账号，请确认微信已登录当前账号")
                return
            sessions = service.load_sessions(account, resolver)
            contacts = service.load_contacts(account, resolver)
            names = {c.user_name: c.display_name for c in contacts}
            self.finished_ok.emit(
                {
                    "root": root,
                    "account": account,
                    "resolver": resolver,
                    "names": names,
                    "sessions": sessions,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 后台线程异常要回传给界面
            self.failed.emit(f"读取失败：{exc}")


class ExportWorker(QThread):
    """后台执行导出。"""

    progress = Signal(int, str)
    finished_ok = Signal(int, int, list)
    failed = Signal(str)

    def __init__(self, account, resolver, sessions, formats, out_dir, media, per_folder):
        super().__init__()
        self.account = account
        self.resolver = resolver
        self.sessions = sessions
        self.formats = formats
        self.out_dir = out_dir
        self.media = media
        self.per_folder = per_folder

    def _emit_progress(self, value: float, text: str) -> None:
        """把 0~1 的进度换算成百分比；-1 表示估不出剩余时间，交给界面滚动。"""
        self.progress.emit(-1 if value < 0 else int(value * 100), text)

    def run(self):  # noqa: D102 - QThread 约定
        try:
            report = service.export_sessions(
                self.account,
                self.resolver,
                self.sessions,
                self.formats,
                self.out_dir,
                copy_media=self.media,
                per_session_folder=self.per_folder,
                progress=self._emit_progress,
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"导出失败：{exc}")
            return
        errors = [
            f"{r.session_user_name} / {r.format}：{r.error}" for r in report.results if not r.ok
        ]
        self.finished_ok.emit(report.ok_count, report.fail_count, errors)


class MainWindow(QWidget):
    """主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("聊天记录导出")
        self._fit_to_screen()
        self.setMinimumSize(820, 520)

        self.data_root: Path | None = None
        self.account = None
        self.resolver = None
        self.names: dict[str, str] = {}
        self.sessions: list[Session] = []
        self.out_dir = locate.default_output_dir()
        self._worker: QThread | None = None
        self._busy = False

        self._build()
        self._refresh_hint("scan")
        self.start_prepare()

    # ------------------------------------------------------------------ 界面
    def _fit_to_screen(self) -> None:
        """按屏幕可用区域定尺寸并居中，避免高 DPI 下窗口超出屏幕。"""
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(1000, 680)
            return
        area = screen.availableGeometry()
        width = min(1000, int(area.width() * 0.9))
        height = min(680, int(area.height() * 0.9))
        self.resize(width, height)
        self.move(area.center().x() - width // 2, area.center().y() - height // 2)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)

        # 状态 + 帮助
        top = QHBoxLayout()
        self.status = QLabel("正在检测微信数据 …")
        self.status.setStyleSheet("color:#185FA5;")
        top.addWidget(self.status, 1)
        help_btn = QPushButton("使用说明 / 帮助")
        help_btn.clicked.connect(self.show_help)
        top.addWidget(help_btn)
        check_btn = QPushButton("环境自检")
        check_btn.setToolTip("收集微信版本、密钥、浏览器等信息，出问题时复制发回")
        check_btn.clicked.connect(self.run_self_check)
        top.addWidget(check_btn)
        outer.addLayout(top)

        # 提示行
        self.hint = QLabel("")
        self.hint.setStyleSheet("color:#b06000; font-weight:bold;")
        outer.addWidget(self.hint)

        # 操作按钮
        bar = QHBoxLayout()
        self.btn_key = QPushButton("① 获取密钥 / 读取会话")
        self.btn_key.clicked.connect(self.start_prepare)
        self.btn_root = QPushButton("数据目录")
        self.btn_root.clicked.connect(self.pick_data_root)
        self.btn_out = QPushButton("输出目录")
        self.btn_out.clicked.connect(self.pick_output)
        self.btn_import = QPushButton("导入密钥")
        self.btn_import.clicked.connect(self.import_key)
        for button in (self.btn_key, self.btn_root, self.btn_import, self.btn_out):
            bar.addWidget(button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(160)
        bar.addWidget(self.progress)
        self.out_label = QLabel(str(self.out_dir))
        self.out_label.setStyleSheet("color:#666;")
        bar.addWidget(self.out_label, 1)
        outer.addLayout(bar)

        # 导出选项
        options = QHBoxLayout()
        options.addWidget(QLabel("格式（可多选）："))
        self.format_boxes: dict[str, QCheckBox] = {}
        for key, label in FORMATS:
            box = QCheckBox(label)
            box.setChecked(key in ("pdf", "html"))
            box.stateChanged.connect(self._update_hint)
            self.format_boxes[key] = box
            options.addWidget(box)
        options.addSpacing(12)
        self.media_box = QCheckBox("还原图片和语音")
        self.media_box.setChecked(True)
        options.addWidget(self.media_box)
        self.folder_box = QCheckBox("每个会话单独建文件夹")
        options.addWidget(self.folder_box)
        options.addStretch(1)
        outer.addLayout(options)

        # 搜索
        search = QHBoxLayout()
        search.addWidget(QLabel("搜索："))
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("昵称 / 备注 / wxid")
        self.keyword.returnPressed.connect(self.filter_sessions)
        search.addWidget(self.keyword, 1)
        for text, slot in (
            ("过滤", self.filter_sessions),
            ("全选", self.select_all),
            ("反选", self.invert_selection),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            search.addWidget(button)
        self.count_label = QLabel("会话 0")
        search.addWidget(self.count_label)
        outer.addLayout(search)

        # 会话列表
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["名称", "username", "最后消息"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_hint)
        outer.addWidget(self.table, 1)

        # 日志 + 导出
        bottom = QHBoxLayout()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        self.log.setFont(QFont("Consolas", 9))
        bottom.addWidget(self.log, 1)
        self.btn_export = QPushButton("③ 导出选中会话")
        self.btn_export.setMinimumHeight(48)
        self.btn_export.clicked.connect(self.start_export)
        bottom.addWidget(self.btn_export)
        outer.addLayout(bottom)
        self._update_hint()

    # ------------------------------------------------------------------ 工具
    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button in (self.btn_key, self.btn_export, self.btn_root, self.btn_import):
            button.setEnabled(not busy)
        if not busy:
            self._update_hint()

    def _refresh_hint(self, key: str) -> None:
        self.hint.setText(HINTS.get(key, ""))

    def _selected_formats(self) -> list[str]:
        return [key for key, box in self.format_boxes.items() if box.isChecked()]

    def _update_hint(self) -> None:
        if self._busy:
            return
        if not self.sessions:
            self._refresh_hint("no_session")
            self.btn_export.setEnabled(False)
        elif not self._selected_formats():
            self.hint.setText("至少要选一种导出格式")
            self.btn_export.setEnabled(False)
        elif not self.table.selectionModel().selectedRows():
            self._refresh_hint("ready")
            self.btn_export.setEnabled(False)
        else:
            self._refresh_hint("ready")
            self.btn_export.setEnabled(True)

    # ------------------------------------------------------------- 数据准备
    def start_prepare(self, manual_key: bytes | None = None) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.progress.setRange(0, 0)
        self.status.setText("正在读取微信数据 …")
        self._refresh_hint("scan")
        self._worker = PrepareWorker(self.data_root, manual_key)
        self._worker.finished_ok.connect(self._on_prepared)
        self._worker.failed.connect(self._on_prepare_failed)
        self._worker.start()

    def _on_prepared(self, payload: dict) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.data_root = payload["root"]
        self.account = payload["account"]
        self.resolver = payload["resolver"]
        self.names = payload["names"]
        self.sessions = payload["sessions"]
        self.status.setText(f"账号 {self.account.display_name} · 数据目录 {self.data_root}")
        self._fill_table(self.sessions)
        self._log(f"读取完成：会话 {len(self.sessions)} 个，联系人 {len(self.names)} 个")
        self._set_busy(False)

    def _on_prepare_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText(message)
        self._log(message)
        self._set_busy(False)

    # ------------------------------------------------------------------ 列表
    def _fill_table(self, sessions: list[Session]) -> None:
        self.table.setRowCount(0)
        for session in sessions:
            row = self.table.rowCount()
            self.table.insertRow(row)
            name = self.names.get(session.user_name, session.user_name)
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(session.user_name))
            last = ""
            if session.order:
                from datetime import datetime

                try:
                    last = datetime.fromtimestamp(session.order).strftime("%Y-%m-%d %H:%M")
                except (OverflowError, OSError, ValueError):
                    last = ""
            self.table.setItem(row, 2, QTableWidgetItem(last))
        self.count_label.setText(f"会话 {len(sessions)}")
        self._update_hint()

    def filter_sessions(self) -> None:
        keyword = self.keyword.text().strip().lower()
        if not keyword:
            self._fill_table(self.sessions)
            return
        matched = [
            s
            for s in self.sessions
            if keyword in self.names.get(s.user_name, "").lower() or keyword in s.user_name.lower()
        ]
        self._fill_table(matched)

    def select_all(self) -> None:
        self.table.selectAll()
        self._update_hint()

    def invert_selection(self) -> None:
        model = self.table.selectionModel()
        flags_off = (
            QItemSelectionModel.SelectionFlag.Deselect | QItemSelectionModel.SelectionFlag.Rows
        )
        flags_on = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for row in range(self.table.rowCount()):
            index = self.table.model().index(row, 0)
            if model.isRowSelected(row):
                model.select(index, flags_off)
            else:
                model.select(index, flags_on)
        self._update_hint()

    def _picked_sessions(self) -> list[tuple[Session, str]]:
        picked = []
        by_name = {s.user_name: s for s in self.sessions}
        for index in sorted({i.row() for i in self.table.selectionModel().selectedRows()}):
            user = self.table.item(index, 1).text()
            session = by_name.get(user)
            if session is not None:
                picked.append((session, self.names.get(user, user)))
        return picked

    # ------------------------------------------------------------------ 导出
    def start_export(self) -> None:
        if self._busy or self.account is None or self.resolver is None:
            return
        picked = self._picked_sessions()
        formats = self._selected_formats()
        if not picked or not formats:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._set_busy(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._refresh_hint("exporting")
        self._log(f"开始导出 {len(picked)} 个会话，格式：{'、'.join(formats)}")
        self._worker = ExportWorker(
            self.account,
            self.resolver,
            picked,
            formats,
            self.out_dir,
            self.media_box.isChecked(),
            self.folder_box.isChecked(),
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_export_done)
        self._worker.failed.connect(self._on_export_failed)
        self._worker.start()

    def _on_progress(self, percent: int, text: str) -> None:
        if percent < 0:
            # 浏览器打印这类步骤估不出剩余时间，切成不确定进度条来回滚动
            if self.progress.maximum() != 0:
                self.progress.setRange(0, 0)
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 100)
            self.progress.setValue(percent)
        if text:
            self.status.setText(text)

    def _on_export_done(self, ok: int, fail: int, errors: list) -> None:
        self._set_busy(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._refresh_hint("done")
        self.status.setText(f"导出完成：成功 {ok} 项，失败 {fail} 项 → {self.out_dir}")
        for line in errors:
            self._log("失败：" + line)
        QMessageBox.information(
            self,
            "导出完成",
            f"成功 {ok} 项，失败 {fail} 项。\n输出目录：{self.out_dir}",
        )

    def _on_export_failed(self, message: str) -> None:
        self._set_busy(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText(message)
        self._log(message)
        QMessageBox.warning(self, "导出失败", message)

    # ------------------------------------------------------------------ 按钮
    def pick_output(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择输出目录", str(self.out_dir))
        if chosen:
            self.out_dir = Path(chosen)
            self.out_label.setText(str(self.out_dir))

    def pick_data_root(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择 xwechat_files 目录", str(Path.home()))
        if not chosen:
            return
        # 用户常常选错一层，这里纠正到真正的数据根目录
        self.data_root = locate.normalize_root(chosen)
        if not locate.is_data_root(self.data_root):
            QMessageBox.warning(
                self,
                "目录不对",
                "这个目录里没有微信账号数据（找不到 wxid_ 开头的文件夹）。\n"
                "请选择微信「文件管理」里的 xwechat_files 文件夹。",
            )
            self.data_root = None
            return
        self._log(f"数据目录已设为 {self.data_root}")
        self.start_prepare()

    def import_key(self) -> None:
        text, ok = QInputDialog.getText(
            self,
            "导入密钥",
            "粘贴 64 位或 96 位 hex（也可粘贴 x'...' 形式）：",
        )
        if not ok or not text.strip():
            return
        key, _salt = parse_hex_key(text)
        if not key:
            QMessageBox.warning(self, "格式不对", "请输入 64 位或 96 位十六进制字符串")
            return
        self._log("使用手动导入的密钥")
        self.start_prepare(manual_key=key)

    def show_help(self) -> None:
        QMessageBox.information(self, "使用说明", HELP_TEXT)

    def run_self_check(self) -> None:
        """收集环境信息并弹窗展示，方便用户整段复制发回。"""
        from wechat_log.core.selfcheck import collect

        self._log("正在自检 …")
        QApplication.processEvents()
        try:
            report = collect()
        except Exception as exc:  # noqa: BLE001 - 自检本身不能把界面打挂
            report = f"自检失败：{exc}"
        self._log("自检完成，点「复制全部」把报告发回即可")

        dialog = QDialog(self)
        dialog.setWindowTitle("环境自检")
        dialog.resize(780, 560)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit(report)
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 9))
        layout.addWidget(view, 1)
        row = QHBoxLayout()
        copy_btn = QPushButton("复制全部")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(report))
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dialog.accept)
        row.addStretch(1)
        row.addWidget(copy_btn)
        row.addWidget(close_btn)
        layout.addLayout(row)
        dialog.exec()
