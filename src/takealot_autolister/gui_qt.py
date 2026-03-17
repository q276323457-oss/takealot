"""
Takealot Auto Lister — PySide6 图形界面

使用方法：
    python -m takealot_autolister.gui_qt
    # 或
    takealot-gui
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from PySide6.QtCore import (
    Qt, QObject, Signal, QThread, QMetaObject, Q_ARG,
    QMutex, QWaitCondition,
)
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .pipeline import process_one_link
from .rules import load_rules


# ── 流水线后台工作线程 ─────────────────────────────────────────────────────────

class _PipelineWorker(QObject):
    """在后台线程执行 process_one_link，通过信号与主线程通信。"""

    log_signal     = Signal(str, str)   # (level, message)
    done_signal    = Signal(object)     # PipelineResult
    error_signal   = Signal(str)
    # 请求主线程打开预览对话框
    preview_request = Signal(object, object)  # (PreviewData, ImageGeneratorSession)

    def __init__(
        self,
        link: str,
        output_dir: Path,
        rules_path: str,
        selectors_path: str,
        headless: bool,
        browser_channel: str,
        user_data_dir: str,
        storage_state_1688: str,
        storage_state_takealot: str,
        browser_profile_directory: str,
        use_llm: bool = True,
        remove_bg: bool = False,
        generate_loadsheet_enabled: bool = True,
    ):
        super().__init__()
        self._link = link
        self._output_dir = output_dir
        self._rules_path = rules_path
        self._selectors_path = selectors_path
        self._headless = headless
        self._browser_channel = browser_channel
        self._user_data_dir = user_data_dir
        self._storage_state_1688 = storage_state_1688
        self._storage_state_takealot = storage_state_takealot
        self._browser_profile_directory = browser_profile_directory
        self._use_llm = use_llm
        self._remove_bg = remove_bg
        self._generate_loadsheet_enabled = generate_loadsheet_enabled

        # 用于阻塞等待预览对话框结果的同步原语
        self._preview_mutex = QMutex()
        self._preview_wait  = QWaitCondition()
        self._preview_result: Any = None

    def deliver_preview_result(self, result: Any) -> None:
        """主线程调用，传回用户在预览对话框中的操作结果。"""
        self._preview_mutex.lock()
        self._preview_result = result
        self._preview_wait.wakeAll()
        self._preview_mutex.unlock()

    def run(self) -> None:
        try:
            rules = load_rules(self._rules_path)

            def _log(level: str, msg: str) -> None:
                self.log_signal.emit(level, msg)

            def _preview_callback(preview_data: Any, img_session: Any) -> Any:
                """从后台线程调用，发射信号让主线程打开对话框，然后阻塞等待结果。"""
                self._preview_mutex.lock()
                self._preview_result = None
                # 通知主线程打开对话框
                self.preview_request.emit(preview_data, img_session)
                # 阻塞等待主线程传回结果（超时 10 分钟）
                self._preview_wait.wait(self._preview_mutex, 600_000)
                result = self._preview_result
                self._preview_mutex.unlock()
                return result

            result = process_one_link(
                link=self._link,
                output_dir=self._output_dir,
                rules=rules,
                use_llm=self._use_llm,
                headless=self._headless,
                browser_channel=self._browser_channel,
                user_data_dir=self._user_data_dir or None,
                storage_state_1688=self._storage_state_1688 or None,
                storage_state_takealot=self._storage_state_takealot or None,
                remove_bg=self._remove_bg,
                automate_portal_enabled=False,
                selectors_path=self._selectors_path or None,
                portal_mode="draft",
                log_callback=_log,
                preview_callback=_preview_callback,
                generate_loadsheet_enabled=self._generate_loadsheet_enabled,
            )
            self.done_signal.emit(result)
        except Exception as e:
            self.error_signal.emit(str(e))


# ── 主窗口 ─────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Takealot Auto Lister")
        self.resize(900, 640)

        self._worker: _PipelineWorker | None = None
        self._worker_thread: QThread | None = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # ── 输入行 ─────────────────────────────────────────────────────────
        input_row = QHBoxLayout()
        self._link_edit = QLineEdit()
        self._link_edit.setPlaceholderText("粘贴 1688 商品链接，然后点击「开始」")
        self._link_edit.returnPressed.connect(self._on_start)
        input_row.addWidget(self._link_edit, stretch=1)

        self._start_btn = QPushButton("▶  开始")
        self._start_btn.setFixedWidth(100)
        self._start_btn.setStyleSheet(
            "QPushButton { background: #1976D2; color: white; border-radius: 4px; padding: 6px 12px; }"
            "QPushButton:hover { background: #1565C0; }"
            "QPushButton:disabled { background: #aaa; }"
        )
        self._start_btn.clicked.connect(self._on_start)
        input_row.addWidget(self._start_btn)

        self._stop_btn = QPushButton("⏹  停止")
        self._stop_btn.setFixedWidth(90)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet("QPushButton { border-radius: 4px; padding: 6px; }")
        self._stop_btn.clicked.connect(self._on_stop)
        input_row.addWidget(self._stop_btn)

        self._reopen_btn = QPushButton("🔄  重新预览")
        self._reopen_btn.setFixedWidth(110)
        self._reopen_btn.setStyleSheet("QPushButton { border-radius: 4px; padding: 6px; }")
        self._reopen_btn.setToolTip("从已有 run 目录重新打开预览对话框（无需重新抓取）")
        self._reopen_btn.clicked.connect(self._on_reopen_preview)
        input_row.addWidget(self._reopen_btn)

        root.addLayout(input_row)

        # ── 配置行（折叠显示）──────────────────────────────────────────────
        cfg_row = QHBoxLayout()
        cfg_row.addWidget(QLabel("规则文件："))
        self._rules_edit = QLineEdit("config/rules.yaml")
        self._rules_edit.setFixedWidth(180)
        cfg_row.addWidget(self._rules_edit)

        cfg_row.addWidget(QLabel("Selectors："))
        self._sel_edit = QLineEdit("config/selectors.yaml")
        self._sel_edit.setFixedWidth(200)
        cfg_row.addWidget(self._sel_edit)

        cfg_row.addWidget(QLabel("输出目录："))
        self._out_edit = QLineEdit("output/runs")
        self._out_edit.setFixedWidth(160)
        cfg_row.addWidget(self._out_edit)

        browse_btn = QPushButton("…")
        browse_btn.setFixedWidth(30)
        browse_btn.clicked.connect(self._browse_output)
        cfg_row.addWidget(browse_btn)
        cfg_row.addStretch()
        root.addLayout(cfg_row)

        # ── 日志区 ─────────────────────────────────────────────────────────
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setFont(QFont("Menlo, Consolas, monospace", 11))
        self._log_box.setStyleSheet(
            "QTextEdit { background: #1e1e1e; color: #d4d4d4; border-radius: 4px; }"
        )
        root.addWidget(self._log_box, stretch=1)

        # ── 状态栏 ─────────────────────────────────────────────────────────
        self._status_lbl = QLabel("就绪")
        self._status_lbl.setStyleSheet("color: #666; padding: 2px;")
        self.statusBar().addWidget(self._status_lbl)

    # ── 槽 ─────────────────────────────────────────────────────────────────

    def _browse_output(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", self._out_edit.text())
        if d:
            self._out_edit.setText(d)

    def _on_start(self) -> None:
        link = self._link_edit.text().strip()
        if not link:
            QMessageBox.warning(self, "提示", "请先输入 1688 商品链接")
            return
        if self._worker_thread and self._worker_thread.isRunning():
            return

        self._log_box.clear()
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_lbl.setText("运行中…")

        output_dir = Path(self._out_edit.text().strip() or "output/runs")
        output_dir.mkdir(parents=True, exist_ok=True)

        self._worker = _PipelineWorker(
            link=link,
            output_dir=output_dir,
            rules_path=self._rules_edit.text().strip() or "config/rules.yaml",
            selectors_path=self._sel_edit.text().strip() or "config/selectors.yaml",
            headless=False,
            browser_channel=os.getenv("BROWSER_CHANNEL", "msedge"),
            user_data_dir=os.getenv("BROWSER_USER_DATA_DIR", ""),
            storage_state_1688=os.getenv("STORAGE_STATE_1688", ""),
            storage_state_takealot=os.getenv("STORAGE_STATE_TAKEALOT", ""),
            browser_profile_directory=os.getenv("BROWSER_PROFILE_DIRECTORY", "Default"),
        )
        self._worker.log_signal.connect(self._on_log)
        self._worker.done_signal.connect(self._on_done)
        self._worker.error_signal.connect(self._on_error)
        self._worker.preview_request.connect(self._on_preview_request)

        self._worker_thread = QThread()
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker_thread.start()

    def _on_stop(self) -> None:
        if self._worker_thread and self._worker_thread.isRunning():
            self._worker_thread.requestInterruption()
            # 如果对话框还在等待，传入 None（取消）
            if self._worker:
                self._worker.deliver_preview_result(None)
        self._reset_buttons()
        self._status_lbl.setText("已停止")

    def _on_log(self, level: str, msg: str) -> None:
        color_map = {
            "ok":   "#4caf50",
            "warn": "#ff9800",
            "error":"#f44336",
            "info": "#d4d4d4",
        }
        color = color_map.get(level, "#d4d4d4")
        self._log_box.append(f'<span style="color:{color};">{msg}</span>')
        self._log_box.moveCursor(QTextCursor.MoveOperation.End)

    def _on_done(self, result: Any) -> None:
        self._reset_buttons()
        action = getattr(result, "action", "")
        ok = getattr(result, "ok", False)
        if ok:
            self._status_lbl.setText(f"✅ 完成：{action}")
        else:
            self._status_lbl.setText(f"⚠️  完成（含警告）：{action}")
        run_dir = getattr(result, "run_dir", "")
        if run_dir:
            self._on_log("info", f"📁 输出：{run_dir}")

    def _on_error(self, msg: str) -> None:
        self._reset_buttons()
        self._status_lbl.setText("❌ 出错")
        self._on_log("error", f"❌ 未处理异常：{msg}")
        QMessageBox.critical(self, "错误", msg[:500])

    def _on_preview_request(self, preview_data: Any, img_session: Any) -> None:
        """主线程：收到后台请求，打开预览对话框。"""
        from .preview_dialog import PreviewDialog
        dlg = PreviewDialog(preview_data, img_session, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.get_result()
        else:
            result = None
        # 把结果传回后台线程
        if self._worker:
            self._worker.deliver_preview_result(result)

    def _on_reopen_preview(self) -> None:
        """从已有 run 目录读取 draft.json / source.json，重新打开预览对话框。"""
        import json
        from .preview_dialog import PreviewData, PreviewDialog
        from .image_generator import ImageGeneratorSession

        # 弹出文件选择，让用户选 run 目录（或自动找最新的）
        output_dir = Path(self._out_edit.text().strip() or "output/runs")
        run_dir_str = QFileDialog.getExistingDirectory(
            self, "选择 run 目录（含 draft.json）", str(output_dir)
        )
        if not run_dir_str:
            return
        run_dir = Path(run_dir_str)

        draft_file  = run_dir / "draft.json"
        source_file = run_dir / "source.json"
        if not draft_file.exists():
            QMessageBox.warning(self, "找不到文件", f"该目录下没有 draft.json：\n{run_dir}")
            return

        try:
            draft_data  = json.loads(draft_file.read_text(encoding="utf-8"))
            source_data = json.loads(source_file.read_text(encoding="utf-8")) if source_file.exists() else {}
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e))
            return

        attrs = draft_data.get("attributes") or {}

        # 还原 portal_fields（若有）
        portal_fields = attrs.get("_probe_fields") or []
        # 还原 category_path
        category_path = attrs.get("_category_path") or source_data.get("category_path") or []
        # 还原图片 URL
        image_urls = source_data.get("image_urls") or []
        # 还原已填字段值（portal label → value，过滤掉内部 _ 开头的 key）
        field_values: dict[str, str] = {
            k: str(v) for k, v in attrs.items()
            if not str(k).startswith("_") and (" " in str(k) or (str(k) and str(k)[0].isupper()))
        }

        preview_data = PreviewData(
            title=draft_data.get("title", ""),
            subtitle=draft_data.get("subtitle", ""),
            source_image_urls=image_urls,
            portal_fields=portal_fields,
            field_values=field_values,
            category_path=category_path,
            product_info=source_data,
        )
        img_session = ImageGeneratorSession(
            source_urls=image_urls,
            product_title=draft_data.get("title", ""),
        )

        dlg = PreviewDialog(preview_data, img_session, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.get_result()
            if result:
                # 生成 loadsheet
                try:
                    from .rules import load_rules
                    from .csv_exporter import generate_loadsheet
                    from .oss_uploader import upload_bytes_list
                    from .types import ListingDraft, ProductSource
                    import dataclasses

                    rules = load_rules(self._rules_edit.text().strip() or "config/rules.yaml")
                    # 重建 ListingDraft（只取 dataclass 定义的字段）
                    draft_fields = {f.name for f in dataclasses.fields(ListingDraft)}
                    draft_obj = ListingDraft(**{k: draft_data[k] for k in draft_fields if k in draft_data})
                    # 把预览结果写回 draft
                    if result.title:
                        draft_obj.title = result.title
                    if result.subtitle:
                        draft_obj.subtitle = result.subtitle
                    for kf_label in ("Key Selling Features", "key_features", "Key Features"):
                        kf_val = (result.field_values or {}).get(kf_label, "").strip()
                        if kf_val:
                            draft_obj.key_features = kf_val
                            break
                    combined = dict(draft_obj.attributes or {})
                    combined.update(result.field_values or {})
                    new_cat_path = [str(x).strip() for x in (getattr(result, "category_path", []) or []) if str(x).strip()]
                    if new_cat_path:
                        combined["_category_path"] = new_cat_path
                    new_probe_fields = getattr(result, "portal_fields", None) or []
                    if isinstance(new_probe_fields, list) and new_probe_fields:
                        combined["_probe_fields"] = new_probe_fields
                    draft_obj.attributes = combined

                    source_fields = {f.name for f in dataclasses.fields(ProductSource)}
                    source_obj = ProductSource(**{k: source_data[k] for k in source_fields if k in source_data}) if source_data else ProductSource(source_url="", title="")

                    # 上传 OSS
                    oss_urls: list[str] = []
                    if result.selected_image_bytes:
                        self._on_log("info", f"☁️  上传 {len(result.selected_image_bytes)} 张图到 OSS...")
                        oss_urls = upload_bytes_list(result.selected_image_bytes, stem="ai_product")

                    xlsm_path = generate_loadsheet(draft_obj, source_obj, run_dir, image_urls=oss_urls or None)
                    if xlsm_path:
                        self._on_log("ok", f"✅ loadsheet 已生成：{xlsm_path.name}")
                        self._on_log("info", f"📁 输出：{run_dir}")
                        self._status_lbl.setText("✅ 重新预览完成")
                    else:
                        self._on_log("warn", "⚠️  xlsm 生成失败（类目找不到对应模板）")
                except Exception as e:
                    self._on_log("error", f"❌ 生成 loadsheet 失败：{e}")
                    QMessageBox.critical(self, "生成失败", str(e))

    def _reset_buttons(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)


# ── 入口 ───────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
