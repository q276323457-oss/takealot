"""
产品预览编辑对话框

布局：
┌─────────────────────────────────────────────────────────┐
│  [类目路径]                                               │
├──────────────────────────┬──────────────────────────────┤
│  [图片区]                │  [文字字段区]                 │
│  - 原图（1688）          │  标题 / 副标题 / portal字段  │
│  - AI 生成白底图         │                               │
├──────────────────────────┴──────────────────────────────┤
│  🤖 AI文字：[输入指令给豆包...          ] [发给豆包生成] │
│  🎨 图片：  [图片修改要求...            ] [重新生成图片] │
│                                    [取消]  [✅ 确认提交] │
└─────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import csv
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QByteArray, QThread, QTimer, Signal, QObject, QPoint, QMimeData
from PySide6.QtGui import QPixmap, QFont, QDrag
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _default_work_root() -> Path:
    if not getattr(sys, "frozen", False):
        return _app_root()
    if sys.platform.startswith("darwin"):
        return Path.home() / "Library" / "Application Support" / "TakealotAutoLister"
    if sys.platform.startswith("win"):
        return Path(os.getenv("APPDATA", str(Path.home()))) / "TakealotAutoLister"
    return Path.home() / ".takealot-autolister"


def _resolve_work_root() -> Path:
    override = os.getenv("TAKEALOT_APP_HOME", "").strip()
    root = Path(override).expanduser() if override else _default_work_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_data_dir(name: str) -> Path:
    root = _app_root()
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(Path(meipass) / name)
    candidates.append(root / name)
    candidates.append(root / "_internal" / name)
    for p in candidates:
        if p.exists():
            return p
    return root / name


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class PreviewData:
    """传入对话框的数据。"""
    title: str = ""
    subtitle: str = ""
    source_image_urls: list[str] = field(default_factory=list)
    portal_fields: list[dict[str, Any]] = field(default_factory=list)   # 来自 probe
    field_values: dict[str, str] = field(default_factory=dict)           # 预填值（可为空）
    category_path: list[str] = field(default_factory=list)
    product_info: dict[str, Any] = field(default_factory=dict)           # 原始采集数据
    run_dir: str = ""                                                     # run 目录路径（用于自动保存）


@dataclass
class PreviewResult:
    """对话框确认后返回的数据。"""
    title: str = ""
    subtitle: str = ""
    selected_image_bytes: list[bytes] = field(default_factory=list)      # 用户选中的最终图片
    field_values: dict[str, str] = field(default_factory=dict)
    category_path: list[str] = field(default_factory=list)                # 最终确认的 Takealot 类目路径
    portal_fields: list[dict[str, Any]] = field(default_factory=list)     # 最终探测字段（用于后续写回）
    confirmed: bool = False


# ── 后台工作线程 ───────────────────────────────────────────────────────────────

class _ImageWorker(QObject):
    """后台生成 AI 白底图。"""
    finished = Signal(list)   # list[bytes]
    error    = Signal(str)

    def __init__(
        self,
        session,
        instruction: str = "",
        is_first: bool = True,
        reference_urls: list[str] | None = None,
        count: int = 4,
    ):
        super().__init__()
        self._session = session
        self._instruction = instruction
        self._is_first = is_first
        self._reference_urls = reference_urls or []
        self._count = count

    def run(self):
        try:
            ref = self._reference_urls or None
            if self._is_first:
                images = self._session.generate(count=self._count, reference_urls=ref)
            else:
                images = self._session.refine(self._instruction, count=self._count, reference_urls=ref)
            self.finished.emit(images)
        except Exception as e:
            self.error.emit(str(e))


class _TranslateWorker(QObject):
    """
    图片中文→英文翻译 worker。
    优先使用易可图 API（专业图片翻译，真正替换文字）。
    若易可图不可用则回退到 VL+PIL 覆盖方案。
    """
    finished = Signal(list)
    partial  = Signal(list)
    error    = Signal(str)

    def __init__(self, images_bytes: list[bytes]):
        super().__init__()
        self._images_bytes = images_bytes

    def run(self):
        print(f"[translate] run() 开始，共 {len(self._images_bytes)} 张")
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from .yiketu import is_available as yiketu_ok, translate_image as yiketu_translate
            from .youdao import is_available as youdao_ok, translate_image as youdao_translate
            from .image_generator import _bytes_to_thumbnail

            if yiketu_ok():
                print("[translate] 使用易可图 API")
                def translate_one(img_bytes: bytes) -> list[bytes]:
                    result = yiketu_translate(img_bytes, source_lang="zh", target_lang="en")
                    return [_bytes_to_thumbnail(result, size=1024)]
            elif youdao_ok():
                print("[translate] 使用有道智云 API")
                def translate_one(img_bytes: bytes) -> list[bytes]:
                    result = youdao_translate(img_bytes, source_lang="zh-CHS", target_lang="en")
                    return [_bytes_to_thumbnail(result, size=1024)]
            else:
                print("[translate] 专业翻译 API 不可用，回退 VL+PIL 方案")
                import io, base64
                from PIL import Image, ImageDraw, ImageFont
                from .siliconflow_llm import call_doubao_vision_url, call_doubao_raw

                _READ_PROMPT = (
                    "List all visible Chinese text in this image, one phrase per line. "
                    "Output ONLY the Chinese text lines, nothing else."
                )

                def _dominant_color(img, box):
                    crop = img.crop(box).convert("RGB").resize((16, 16), Image.LANCZOS)
                    px = list(crop.getdata())
                    return tuple(sum(c[i] for c in px) // len(px) for i in range(3))

                def _overlay_english(img_b: bytes, en_lines: list[str]) -> bytes:
                    img = Image.open(io.BytesIO(img_b)).convert("RGB")
                    w, h = img.size
                    px2, py2 = int(w * 0.48), int(h * 0.65)
                    bg = _dominant_color(img, (0, 0, px2, py2))
                    bg_fill = tuple(max(0, c - 18) for c in bg)
                    brightness = (bg[0]*299 + bg[1]*587 + bg[2]*114) // 1000
                    txt_color = (20, 20, 20) if brightness > 128 else (235, 235, 235)
                    draw = ImageDraw.Draw(img)
                    draw.rectangle([(0, 0), (px2, py2)], fill=bg_fill)
                    font_lg_sz = max(26, h // 15)
                    font_sm_sz = max(17, h // 28)
                    try:
                        font_lg = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_lg_sz)
                        font_sm = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_sm_sz)
                    except Exception:
                        font_lg = font_sm = ImageFont.load_default()
                    margin = max(14, w // 38)
                    y = margin
                    for i, line in enumerate(en_lines[:7]):
                        line = line.strip()
                        if not line:
                            continue
                        font = font_lg if i == 0 else font_sm
                        sz = font_lg_sz if i == 0 else font_sm_sz
                        max_w = px2 - margin * 2
                        while len(line) > 3:
                            if draw.textbbox((0, 0), line, font=font)[2] <= max_w:
                                break
                            line = line[:-1]
                        draw.text((margin, y), line, fill=txt_color, font=font)
                        y += sz + 10
                        if y > py2 - sz:
                            break
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=88)
                    return buf.getvalue()

                def translate_one(img_bytes: bytes) -> list[bytes]:
                    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                    if max(img.size) > 1024:
                        img.thumbnail((1024, 1024), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    small = buf.getvalue()
                    b64 = base64.b64encode(small).decode()
                    data_url = f"data:image/jpeg;base64,{b64}"
                    zh_text = call_doubao_vision_url(data_url, _READ_PROMPT).strip()
                    if not zh_text:
                        return [_bytes_to_thumbnail(img_bytes, size=1024)]
                    en_raw = call_doubao_raw(
                        f"Translate these Chinese product marketing phrases to concise English. "
                        f"One English phrase per line:\n{zh_text}",
                        temperature=0.1,
                    ).strip()
                    en_lines = [l for l in en_raw.splitlines() if l.strip()]
                    return [_bytes_to_thumbnail(_overlay_english(small, en_lines), size=1024)]

            total = len(self._images_bytes)
            all_results: list[bytes] = []
            with ThreadPoolExecutor(max_workers=total) as executor:
                futures = [executor.submit(translate_one, b) for b in self._images_bytes]
                for fut in as_completed(futures):
                    try:
                        imgs = fut.result()
                        all_results.extend(imgs)
                        self.partial.emit(imgs)
                    except Exception as e:
                        print(f"[translate] 单张失败: {e}")
            self.finished.emit(all_results)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error.emit(str(e))


class _ListingWorker(QObject):
    """后台调用 AI 生成标题/描述/参数。"""
    finished = Signal(dict)   # 解析后的 JSON dict
    error    = Signal(str)

    def __init__(self, product_info: dict, portal_fields: list, user_instructions: str):
        super().__init__()
        self._product_info = product_info
        self._portal_fields = portal_fields
        self._instructions = user_instructions

    def run(self):
        try:
            from .llm import generate_listing_with_instructions
            result = generate_listing_with_instructions(
                self._product_info,
                self._portal_fields,
                self._instructions,
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class _CategoryProbeWorker(QObject):
    """后台探测类目字段（优先使用手动输入的 Takealot 类目）。"""
    finished = Signal(dict)   # {"source_category_path": [...], "resolved_category_path": [...], "probe_result": {...}}
    error = Signal(str)

    def __init__(
        self,
        manual_takealot_path: list[str],
        fallback_source_category_path: list[str],
        product_title: str,
        selectors_cfg_path: Path,
        storage_state_takealot_path: str | Path | None = None,
    ):
        super().__init__()
        self._manual_takealot_path = [str(x).strip() for x in manual_takealot_path if str(x).strip()]
        self._fallback_source_category_path = [str(x).strip() for x in fallback_source_category_path if str(x).strip()]
        self._product_title = str(product_title or "").strip()
        self._selectors_cfg_path = Path(selectors_cfg_path)
        self._storage_state_takealot_path = (
            Path(storage_state_takealot_path).expanduser()
            if storage_state_takealot_path
            else None
        )

    @staticmethod
    def _contains_zh(parts: list[str]) -> bool:
        import re as _re
        text = " ".join(str(x) for x in parts)
        return bool(_re.search(r"[\u4e00-\u9fff]", text))

    def run(self):
        try:
            import os
            from .portal import find_probe_category_path, probe_category_fields

            manual_path = [str(x).strip() for x in self._manual_takealot_path if str(x).strip()]
            fallback_source_path = [str(x).strip() for x in self._fallback_source_category_path if str(x).strip()]
            if not manual_path and not fallback_source_path:
                raise RuntimeError("类目不能为空，请先输入 Takealot 类目。")

            # 优先用手动输入的 Takealot 路径；若输入中文/短路径则走自动映射。
            source_path_for_override = fallback_source_path or manual_path
            resolved_path: list[str] = []
            source_for_match = manual_path if manual_path else fallback_source_path
            if self._contains_zh(source_for_match) or len(source_for_match) <= 2:
                resolved_path = find_probe_category_path(
                    source_category_path=source_for_match,
                    source_title=self._product_title,
                    selectors_cfg_path=self._selectors_cfg_path,
                )
            if not resolved_path:
                resolved_path = manual_path or fallback_source_path

            browser_channel = os.getenv("BROWSER_CHANNEL", "msedge")
            user_data_dir = os.getenv("BROWSER_USER_DATA_DIR") or None
            env_state_takealot = os.getenv("STORAGE_STATE_TAKEALOT", "").strip()
            storage_state_takealot = env_state_takealot or (
                str(self._storage_state_takealot_path) if self._storage_state_takealot_path else None
            )
            storage_state_takealot_exists = bool(
                storage_state_takealot and Path(storage_state_takealot).exists()
            )
            print(
                f"[preview_probe] storage_state_takealot={storage_state_takealot} "
                f"exists={storage_state_takealot_exists}"
            )
            browser_profile_directory = os.getenv("BROWSER_PROFILE_DIRECTORY", "Default")
            headless_env = str(os.getenv("DEFAULT_HEADLESS", "true")).strip().lower()
            headless = headless_env in {"1", "true", "yes", "y", "on"}

            probe_result = probe_category_fields(
                category_path=resolved_path,
                selectors_cfg_path=self._selectors_cfg_path,
                headless=headless,
                browser_channel=browser_channel,
                user_data_dir=user_data_dir,
                storage_state_path=storage_state_takealot,
                browser_profile_directory=browser_profile_directory,
                force_refresh=True,
            )
            self.finished.emit(
                {
                    "source_category_path": source_path_for_override,
                    "resolved_category_path": [str(x).strip() for x in (probe_result.get("category_path") or resolved_path) if str(x).strip()],
                    "probe_result": probe_result,
                    "storage_state_takealot_path": storage_state_takealot or "",
                    "storage_state_takealot_exists": storage_state_takealot_exists,
                }
            )
        except Exception as e:
            self.error.emit(str(e))


# ── 图片缩略图卡片 ────────────────────────────────────────────────────────────

class _ImagePreviewDialog(QDialog):
    """大图预览弹窗，双击图片卡片时弹出。"""

    def __init__(self, img_bytes: bytes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("图片预览")
        self.setModal(True)

        # 计算适合屏幕的最大尺寸
        screen = self.screen()
        if screen:
            available = screen.availableGeometry()
            max_size = min(available.width() - 80, available.height() - 120, 900)
        else:
            max_size = 800

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        lbl = QLabel()
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ba = QByteArray(img_bytes)
        pm = QPixmap()
        pm.loadFromData(ba)
        if not pm.isNull():
            pm = pm.scaled(max_size, max_size, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        lbl.setPixmap(pm)
        layout.addWidget(lbl)

        btn = QPushButton("关闭")
        btn.setFixedWidth(100)
        btn.clicked.connect(self.accept)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)

        self.adjustSize()


class _ImageCard(QFrame):
    """单张图片卡片，点击切换选中状态，拖拽可调整顺序。"""

    # 自定义 MIME 类型，存放源卡片的 Python 对象 id（整数字符串）
    _MIME_TYPE = "application/x-imgcard-id"

    def __init__(self, img_bytes: bytes, index: int, parent=None, full_bytes: bytes | None = None,
                 reorder_callback=None):
        super().__init__(parent)
        self._bytes = img_bytes
        self._full_bytes = full_bytes if full_bytes is not None else img_bytes
        self._index = index
        self._selected = False
        self._reorder_callback = reorder_callback   # callable(src_id, tgt_id) or None
        self._drag_start_pos: QPoint | None = None
        self._did_drag = False

        self.setFixedSize(160, 185)
        self.setFrameShape(QFrame.Shape.Box)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._lbl = QLabel()
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl.setFixedSize(150, 150)
        pixmap = self._bytes_to_pixmap(img_bytes, 150)
        self._lbl.setPixmap(pixmap)
        layout.addWidget(self._lbl)

        self._chk = QCheckBox("选用")
        self._chk.stateChanged.connect(self._on_check)
        layout.addWidget(self._chk, alignment=Qt.AlignmentFlag.AlignCenter)

        self._update_border()

    @staticmethod
    def _bytes_to_pixmap(data: bytes, size: int) -> QPixmap:
        ba = QByteArray(data)
        pm = QPixmap()
        pm.loadFromData(ba)
        if not pm.isNull():
            pm = pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        return pm

    def _on_check(self, state):
        self._selected = bool(state)
        self._update_border()

    def _update_border(self):
        color = "#2196F3" if self._selected else "#cccccc"
        self.setStyleSheet(f"QFrame {{ border: 2px solid {color}; border-radius: 4px; }}")

    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, v: bool):
        self._chk.setChecked(v)

    def get_bytes(self) -> bytes:
        return self._bytes

    def get_full_bytes(self) -> bytes:
        """返回完整尺寸 bytes（原图选中时用于上传 OSS）。"""
        return self._full_bytes

    # ── 拖拽支持 ─────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
            self._did_drag = False

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._drag_start_pos is None:
            return
        from PySide6.QtWidgets import QApplication
        if (event.pos() - self._drag_start_pos).manhattanLength() < QApplication.startDragDistance():
            return
        # 达到拖拽距离 → 启动 QDrag
        self._did_drag = True
        mime = QMimeData()
        mime.setData(self._MIME_TYPE, str(id(self)).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        # 用卡片截图作为拖拽预览，缩小到 80px 宽
        pix = self.grab().scaledToWidth(80, Qt.TransformationMode.SmoothTransformation)
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
        drag.exec(Qt.DropAction.MoveAction)

    def mouseReleaseEvent(self, event):
        # 只有没有发生拖拽时，才视为"点击"来切换选中
        if event.button() == Qt.MouseButton.LeftButton and not self._did_drag:
            self._chk.setChecked(not self._chk.isChecked())
        self._drag_start_pos = None
        self._did_drag = False

    def mouseDoubleClickEvent(self, event):
        dlg = _ImagePreviewDialog(self._full_bytes, parent=self.window())
        dlg.exec()

    # ── 接收拖拽放置 ─────────────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(self._MIME_TYPE):
            src_id = int(event.mimeData().data(self._MIME_TYPE).toStdString())
            if src_id != id(self):          # 不允许放到自身
                self.setStyleSheet(
                    "QFrame { border: 2px dashed #FF9800; border-radius: 4px; }"
                )
                event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._update_border()

    def dropEvent(self, event):
        self._update_border()
        if not event.mimeData().hasFormat(self._MIME_TYPE):
            return
        src_id = int(event.mimeData().data(self._MIME_TYPE).toStdString())
        tgt_id = id(self)
        if src_id != tgt_id and self._reorder_callback:
            self._reorder_callback(src_id, tgt_id)
        event.acceptProposedAction()


class _CategoryPickerDialog(QDialog):
    """Takealot 类目三栏选择器（Division / Department / Leaf）。"""

    def __init__(self, records: list[dict[str, Any]], current_path: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择 Takealot 类目")
        self.resize(1120, 560)
        self.setModal(True)

        self._records = records or []
        self._current_path = [str(x).strip() for x in (current_path or []) if str(x).strip()]
        self._selected_path: list[str] = []
        self._tree: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._build_tree()

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        hdr = QLabel("<b>按 Takealot 后台风格选择类目</b>")
        root.addWidget(hdr)

        top = QHBoxLayout()
        top.addWidget(QLabel("搜索"))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索任意类目关键字（中英文）")
        self._search_edit.textChanged.connect(self._refresh_leaf_list)
        top.addWidget(self._search_edit, stretch=1)
        root.addLayout(top)

        cols = QHBoxLayout()
        cols.setSpacing(8)
        self._division_list = QListWidget()
        self._division_list.setMinimumWidth(260)
        self._department_list = QListWidget()
        self._department_list.setMinimumWidth(320)
        self._leaf_list = QListWidget()
        self._leaf_list.setMinimumWidth(420)
        cols.addWidget(self._division_list, stretch=2)
        cols.addWidget(self._department_list, stretch=3)
        cols.addWidget(self._leaf_list, stretch=4)
        root.addLayout(cols, stretch=1)

        self._selected_lbl = QLabel("未选择")
        self._selected_lbl.setStyleSheet("color:#555;")
        root.addWidget(self._selected_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("使用此类目")
        ok_btn.setStyleSheet(
            "QPushButton { background: #1976D2; color: white; border-radius: 4px; padding: 6px 12px; }"
            "QPushButton:hover { background: #1565C0; }"
        )
        ok_btn.clicked.connect(self._accept_if_selected)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        root.addLayout(btn_row)

        # 用当前行变化触发，内部通过 UserRole 取原始 division key
        self._division_list.currentRowChanged.connect(lambda _idx: self._on_division_changed())
        self._department_list.currentTextChanged.connect(self._refresh_leaf_list)
        self._leaf_list.currentTextChanged.connect(self._on_leaf_changed)

        self._populate_divisions()
        self._apply_initial_selection()

    @staticmethod
    def _leaf_title(rec: dict[str, Any]) -> str:
        main_en = str(rec.get("main", "")).strip()
        low_en = str(rec.get("lowest", "")).strip()
        main_zh = str(rec.get("main_zh", "")).strip()
        low_zh = str(rec.get("lowest_zh", "")).strip()
        main = main_zh or main_en
        lowest = low_zh or low_en
        if lowest and lowest != main:
            return f"{main} -> {lowest}"
        return main or lowest

    @staticmethod
    def _norm(s: str) -> str:
        import re as _re
        return " ".join(_re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", " ", str(s or "").lower()).split())

    def _build_tree(self) -> None:
        tree: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._division_labels: dict[str, str] = {}
        self._department_labels: dict[tuple[str, str], str] = {}
        for rec in self._records:
            if not isinstance(rec, dict):
                continue
            division = str(rec.get("division", "")).strip()
            department = str(rec.get("department", "")).strip()
            if not division or not department:
                continue
            tree.setdefault(division, {}).setdefault(department, []).append(rec)
            d_zh = str(rec.get("division_zh", "")).strip()
            dept_zh = str(rec.get("department_zh", "")).strip()
            if d_zh and division not in self._division_labels:
                self._division_labels[division] = f"{division}（{d_zh}）"
            if dept_zh and (division, department) not in self._department_labels:
                self._department_labels[(division, department)] = f"{department}（{dept_zh}）"
        self._tree = tree

    def _populate_divisions(self) -> None:
        self._division_list.clear()
        for d in sorted(self._tree.keys()):
            label = self._division_labels.get(d, d)
            self._division_list.addItem(label)
            self._division_list.item(self._division_list.count() - 1).setData(Qt.ItemDataRole.UserRole, d)
        if self._division_list.count() > 0:
            self._division_list.setCurrentRow(0)

    def _on_division_changed(self) -> None:
        self._department_list.clear()
        item = self._division_list.currentItem()
        if not item:
            self._leaf_list.clear()
            return
        division = item.data(Qt.ItemDataRole.UserRole) or item.text().strip()
        deps = sorted((self._tree.get(division) or {}).keys())
        for d in deps:
            label = self._department_labels.get((division, d), d)
            self._department_list.addItem(label)
            self._department_list.item(self._department_list.count() - 1).setData(Qt.ItemDataRole.UserRole, d)
        if self._department_list.count() > 0:
            self._department_list.setCurrentRow(0)
        else:
            self._leaf_list.clear()

    def _refresh_leaf_list(self) -> None:
        self._leaf_list.clear()
        query = self._norm(self._search_edit.text())

        # 全局搜索：有搜索词时，在所有记录里搜索（不限制 division/department）
        if query:
            rows = list(self._records)
            rows.sort(key=lambda x: self._leaf_title(x).lower())
            for rec in rows:
                txt = self._leaf_title(rec)
                all_txt = " ".join(
                    [
                        str(rec.get("division", "")),
                        str(rec.get("department", "")),
                        str(rec.get("main", "")),
                        str(rec.get("lowest", "")),
                        str(rec.get("division_zh", "")),
                        str(rec.get("department_zh", "")),
                        str(rec.get("main_zh", "")),
                        str(rec.get("lowest_zh", "")),
                    ]
                )
                hay = self._norm(f"{txt} {all_txt}")
                if query not in hay:
                    continue
                self._leaf_list.addItem(txt)
                self._leaf_list.item(self._leaf_list.count() - 1).setData(
                    Qt.ItemDataRole.UserRole, rec.get("path") or []
                )
            if self._leaf_list.count() > 0:
                self._leaf_list.setCurrentRow(0)
            else:
                self._selected_path = []
                self._selected_lbl.setText("未匹配到类目，请更换关键字")
            return

        # 无搜索词：仅在当前 division / department 下列出
        div_item = self._division_list.currentItem()
        dept_item = self._department_list.currentItem()
        division = div_item.data(Qt.ItemDataRole.UserRole) if div_item else ""
        department = dept_item.data(Qt.ItemDataRole.UserRole) if dept_item else ""
        if not division or not department:
            return
        rows = list((self._tree.get(division) or {}).get(department) or [])
        rows.sort(key=lambda x: self._leaf_title(x).lower())
        for rec in rows:
            txt = self._leaf_title(rec)
            self._leaf_list.addItem(txt)
            self._leaf_list.item(self._leaf_list.count() - 1).setData(
                Qt.ItemDataRole.UserRole, rec.get("path") or []
            )
        if self._leaf_list.count() > 0:
            self._leaf_list.setCurrentRow(0)

    def _on_leaf_changed(self, _text: str) -> None:
        item = self._leaf_list.currentItem()
        if not item:
            self._selected_path = []
            self._selected_lbl.setText("未选择")
            return
        path = item.data(Qt.ItemDataRole.UserRole) or []
        self._selected_path = [str(x).strip() for x in path if str(x).strip()]
        if self._selected_path:
            self._selected_lbl.setText("已选择: " + " > ".join(self._selected_path))
        else:
            self._selected_lbl.setText("未选择")

    def _apply_initial_selection(self) -> None:
        if not self._current_path:
            return
        d = self._current_path[0] if len(self._current_path) > 0 else ""
        dep = self._current_path[1] if len(self._current_path) > 1 else ""
        if d:
            for i in range(self._division_list.count()):
                if self._division_list.item(i).text().strip().lower() == d.lower():
                    self._division_list.setCurrentRow(i)
                    break
        if dep:
            for i in range(self._department_list.count()):
                if self._department_list.item(i).text().strip().lower() == dep.lower():
                    self._department_list.setCurrentRow(i)
                    break
        if self._current_path:
            cur_norm = " > ".join(self._current_path).strip().lower()
            for i in range(self._leaf_list.count()):
                item = self._leaf_list.item(i)
                path = [str(x).strip() for x in (item.data(Qt.ItemDataRole.UserRole) or []) if str(x).strip()]
                if " > ".join(path).strip().lower() == cur_norm:
                    self._leaf_list.setCurrentRow(i)
                    break

    def _accept_if_selected(self) -> None:
        if not self._selected_path:
            QMessageBox.warning(self, "提示", "请先选择一个类目。")
            return
        self.accept()

    def selected_path(self) -> list[str]:
        return [str(x).strip() for x in self._selected_path if str(x).strip()]


# ── 主对话框 ──────────────────────────────────────────────────────────────────

class PreviewDialog(QDialog):
    """
    产品预览编辑对话框。

    参数：
        data:          PreviewData，包含原始产品信息/portal字段/原图URL等
        image_session: ImageGeneratorSession 实例（或 None，则不显示图片生成区）
        parent:        父窗口
    """

    # 跨线程安全信号：后台线程下载完缩略图后通知主线程刷新
    _src_thumbs_ready = Signal(list)   # list[bytes]

    def __init__(self, data: PreviewData, image_session=None, parent=None, run_dir=None):
        super().__init__(parent)
        self.setWindowTitle("预览 & 编辑 — AI 一键上品")
        self.resize(1200, 780)
        self.setModal(True)

        self._data = data
        self._session = image_session
        # run_dir 优先从参数取，其次从 data.run_dir 取
        _rd = run_dir or data.run_dir or None
        self._run_dir = Path(_rd) if _rd else None
        self._generated_cards: list[_ImageCard] = []
        self._src_image_cards: list[_ImageCard] = []   # 原图卡片（可勾选）
        self._img_worker_thread: QThread | None = None
        self._txt_worker_thread: QThread | None = None
        self._translate_thread: QThread | None = None
        self._probe_thread: QThread | None = None
        self._result = PreviewResult()
        self._app_root = _app_root()
        self._work_root = _resolve_work_root()
        self._config_root = _resolve_data_dir("config")
        self._input_root = _resolve_data_dir("input")
        self._storage_state_takealot_path = Path(
            os.getenv(
                "STORAGE_STATE_TAKEALOT",
                str(self._work_root / ".runtime" / "auth" / "takealot.json"),
            )
        ).expanduser()
        self._selectors_cfg_path = self._config_root / "selectors.yaml"
        src_path = (self._data.product_info or {}).get("category_path") or []
        self._source_category_path = [str(x).strip() for x in src_path if str(x).strip()]
        self._pending_reprobe_form: dict[str, Any] = {}

        # 防抖自动保存定时器：字段变动后 1.5 秒写盘
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(1500)
        self._autosave_timer.timeout.connect(self.autosave)

        # 连接原图缩略图信号（跨线程安全）
        self._src_thumbs_ready.connect(self._load_source_thumbnails)

        self._build_ui()

        # 用 1688 已有属性预填已知 portal 字段（型号/重量/颜色等）
        self._prefill_from_product_attrs()

        # 恢复上次自动保存的草稿（若 run_dir 下存在 preview_autosave.json）
        if self._run_dir:
            self._restore_autosave()

        # 把所有可编辑字段的变动信号连接到防抖定时器
        self._connect_autosave_signals()

        # 后台加载原图缩略图（不自动生成 AI 图，等用户手动触发）
        if self._session:
            threading.Thread(target=self._load_source_async, daemon=True).start()

    # ── UI 构建 ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        # 顶部类目编辑区（手动修正 Takealot 类目）
        cat_box = QVBoxLayout()
        cat_box.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>类目（手动编辑 Takealot）</b>"))
        header.addStretch()
        self._cat_status = QLabel("就绪")
        self._cat_status.setStyleSheet("color: #666;")
        header.addWidget(self._cat_status)
        cat_box.addLayout(header)

        row_src = QHBoxLayout()
        row_src.addWidget(QLabel("1688类目(参考)"))
        self._source_category_view = QLineEdit(self._fmt_path(self._source_category_path))
        self._source_category_view.setReadOnly(True)
        self._source_category_view.setStyleSheet("color: #666; background: #f5f5f5;")
        row_src.addWidget(self._source_category_view, stretch=1)
        cat_box.addLayout(row_src)

        row = QHBoxLayout()
        row.addWidget(QLabel("Takealot类目"))
        self._takealot_category_edit = QLineEdit(self._initial_takealot_category_text())
        self._takealot_category_edit.setPlaceholderText("例：Consumer Electronics > Electronic Accessories > Cellphone Headsets")
        self._takealot_category_edit.textChanged.connect(self._schedule_autosave)
        row.addWidget(self._takealot_category_edit, stretch=1)
        self._cat_browse_btn = QPushButton("浏览…")
        self._cat_browse_btn.setFixedWidth(70)
        self._cat_browse_btn.clicked.connect(self._open_category_picker)
        row.addWidget(self._cat_browse_btn)
        self._cat_save_probe_btn = QPushButton("💾 保存并重探测字段")
        self._cat_save_probe_btn.setFixedWidth(170)
        self._cat_save_probe_btn.clicked.connect(self._on_save_and_reprobe_category)
        row.addWidget(self._cat_save_probe_btn)
        cat_box.addLayout(row)

        self._takealot_cat_label = QLabel("")
        self._takealot_cat_label.setStyleSheet("color: #555;")
        cat_box.addWidget(self._takealot_cat_label)
        self._refresh_takealot_category_label()

        root.addLayout(cat_box)

        # 主体分割器
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, stretch=1)

        splitter.addWidget(self._build_image_panel())
        splitter.addWidget(self._build_fields_panel())
        splitter.setSizes([620, 560])

        # 底部 AI 区域
        root.addWidget(self._build_bottom_bar())

    def _initial_takealot_category_text(self) -> str:
        if self._data.category_path:
            return " > ".join(str(x) for x in self._data.category_path if str(x).strip())
        if self._source_category_path:
            return " > ".join(self._source_category_path)
        return ""

    @staticmethod
    def _parse_category_text(text: str) -> list[str]:
        import re as _re
        parts = _re.split(r"[>›]+", str(text or ""))
        return [str(x).strip() for x in parts if str(x).strip()]

    @staticmethod
    def _fmt_path(path: list[str]) -> str:
        vals = [str(x).strip() for x in path if str(x).strip()]
        return " > ".join(vals) if vals else "（未匹配）"

    def _refresh_takealot_category_label(self) -> None:
        self._takealot_cat_label.setText(f"<b>当前Takealot：</b>{self._fmt_path(self._data.category_path)}")

    def _collect_current_field_values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        seen_widgets = set()
        for label, w in getattr(self, "_field_widgets", {}).items():
            wid = id(w)
            if wid in seen_widgets:
                continue
            seen_widgets.add(wid)
            if isinstance(w, QComboBox):
                val = w.currentText().strip()
            elif isinstance(w, QTextEdit):
                val = w.toPlainText().strip()
            else:
                val = w.text().strip()
            if val:
                out[str(label)] = val
        return out

    def _load_category_records(self) -> list[dict[str, Any]]:
        """从 takealot_categories.csv 读取类目树，用于弹窗选择。"""
        # 兼容两种项目结构（根目录下有 src/ 或直接是包目录）：
        # 自底向上查找最近的 input/takealot_categories.csv。
        csv_path = None
        search_roots = [self._input_root, self._work_root / "input", self._app_root / "input", *Path(__file__).resolve().parents]
        for root in search_roots:
            root = Path(root)
            candidate = root / "takealot_categories.csv" if root.name == "input" else root / "input" / "takealot_categories.csv"
            if candidate.exists():
                csv_path = candidate
                break
        if csv_path is None:
            return []
        records: list[dict[str, Any]] = []
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                # 跳过前两行说明，定位到真正的表头行（以 Division 开头）
                header_found = False
                for row in reader:
                    if not row:
                        continue
                    if str(row[0]).strip() == "Division":
                        header_found = True
                        break
                if not header_found:
                    return []
                for row in reader:
                    if not row or len(row) < 4:
                        continue
                    division = str(row[0]).strip()
                    dept = str(row[1]).strip()
                    main = str(row[2]).strip()
                    lowest_raw = str(row[3]).strip()
                    if not division or not dept:
                        continue
                    d_zh = str(row[6]).strip() if len(row) > 6 else ""
                    dept_zh = str(row[7]).strip() if len(row) > 7 else ""
                    main_zh = str(row[8]).strip() if len(row) > 8 else ""
                    low_zh = str(row[9]).strip() if len(row) > 9 else ""
                    lowest_parts = [p.strip() for p in lowest_raw.split("->") if p.strip()]
                    path = [division, dept]
                    if main:
                        path.append(main)
                    if lowest_parts:
                        path.extend(lowest_parts)
                    records.append(
                        {
                            "division": division,
                            "department": dept,
                            "main": main,
                            "lowest": lowest_parts[-1] if lowest_parts else lowest_raw,
                            "division_zh": d_zh,
                            "department_zh": dept_zh,
                            "main_zh": main_zh,
                            "lowest_zh": low_zh,
                            "path": path,
                        }
                    )
        except Exception:
            return []
        return records

    def _open_category_picker(self) -> None:
        records = self._load_category_records()
        if not records:
            QMessageBox.warning(self, "无法打开", "找不到 input/takealot_categories.csv，无法显示类目列表。")
            return
        dlg = _CategoryPickerDialog(records, current_path=self._data.category_path, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        path = dlg.selected_path()
        if not path:
            return
        self._data.category_path = path
        self._takealot_category_edit.setText(self._fmt_path(path))
        self._refresh_takealot_category_label()
        self._schedule_autosave()

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            child = item.layout()
            if child is not None:
                self._clear_layout(child)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild_field_rows(self, title: str, subtitle: str, field_values: dict[str, str]) -> None:
        self._data.title = title
        self._data.subtitle = subtitle
        self._data.field_values = dict(field_values or {})
        self._field_widgets = {}
        self._clear_layout(self._fields_layout)
        self._build_field_rows()
        self._connect_autosave_signals()

    def _on_save_and_reprobe_category(self) -> None:
        if self._probe_thread and self._probe_thread.isRunning():
            QMessageBox.information(self, "提示", "正在探测中，请稍候。")
            return
        manual_takealot_path = self._parse_category_text(self._takealot_category_edit.text().strip())
        if not manual_takealot_path:
            QMessageBox.warning(self, "提示", "请先输入 Takealot 类目。")
            return

        self._pending_reprobe_form = {
            "title": self._title_edit.text().strip() if hasattr(self, "_title_edit") else "",
            "subtitle": self._subtitle_edit.text().strip() if hasattr(self, "_subtitle_edit") else "",
            "field_values": self._collect_current_field_values(),
        }

        self._cat_save_probe_btn.setEnabled(False)
        self._cat_status.setText("探测中…")
        self._cat_status.setStyleSheet("color: #1565C0;")

        product_title = str((self._data.product_info or {}).get("title", "") or self._data.title).strip()
        self._probe_thread = QThread()
        self._probe_worker = _CategoryProbeWorker(
            manual_takealot_path=manual_takealot_path,
            fallback_source_category_path=self._source_category_path,
            product_title=product_title,
            selectors_cfg_path=self._selectors_cfg_path,
            storage_state_takealot_path=self._storage_state_takealot_path,
        )
        self._probe_worker.moveToThread(self._probe_thread)
        self._probe_thread.started.connect(self._probe_worker.run)
        self._probe_worker.finished.connect(self._on_category_reprobe_done)
        self._probe_worker.error.connect(self._on_category_reprobe_error)
        self._probe_worker.finished.connect(self._probe_thread.quit)
        self._probe_worker.error.connect(self._probe_thread.quit)
        self._probe_thread.finished.connect(lambda: self._cat_save_probe_btn.setEnabled(True))
        self._probe_thread.start()

    def _on_category_reprobe_done(self, payload: dict[str, Any]) -> None:
        source_path = [str(x).strip() for x in (payload.get("source_category_path") or []) if str(x).strip()]
        resolved_path = [str(x).strip() for x in (payload.get("resolved_category_path") or []) if str(x).strip()]
        probe_result = payload.get("probe_result") or {}
        fields = probe_result.get("fields") or []
        if not resolved_path:
            self._on_category_reprobe_error("重探测失败：未解析出有效 Takealot 类目路径")
            return
        # 检测未登录错误（无需启动浏览器时的快速失败）
        if probe_result.get("error") == "need_login":
            state_path = str(payload.get("storage_state_takealot_path") or "").strip()
            state_exists = bool(payload.get("storage_state_takealot_exists"))
            state_hint = (
                f"\n探测使用登录态文件：{state_path or '未设置'}（{'存在' if state_exists else '不存在'}）"
            )
            self._on_category_reprobe_error(
                "未登录 Takealot，无法探测字段。\n请在主界面点击「登录 Takealot 卖家后台」后重试。"
                + state_hint
            )
            return

        # 保存记忆映射（等同“记住当前类目”）
        save_ok, save_msg = self._save_category_override(
            source_category_path=source_path,
            takealot_path=resolved_path,
        )

        # 更新当前数据并重建字段区
        self._source_category_path = source_path or self._source_category_path
        self._data.category_path = resolved_path
        self._data.portal_fields = fields
        if hasattr(self, "_takealot_category_edit"):
            self._takealot_category_edit.setText(self._fmt_path(resolved_path))
        self._refresh_takealot_category_label()

        form = self._pending_reprobe_form or {}
        self._rebuild_field_rows(
            title=str(form.get("title", "")),
            subtitle=str(form.get("subtitle", "")),
            field_values=dict(form.get("field_values") or {}),
        )
        self._pending_reprobe_form = {}

        n_fields = len(fields)
        self._cat_status.setText(f"已重探测 {n_fields} 个字段")
        self._cat_status.setStyleSheet("color: #2E7D32;")
        self._schedule_autosave()
        if not save_ok:
            QMessageBox.warning(self, "类目映射保存失败", save_msg)

    def _on_category_reprobe_error(self, msg: str) -> None:
        self._cat_status.setText("重探测失败")
        self._cat_status.setStyleSheet("color: #C62828;")
        QMessageBox.warning(self, "重探测失败", str(msg)[:500])

    def _save_category_override(self, source_category_path: list[str], takealot_path: list[str]) -> tuple[bool, str]:
        src = [str(x).strip() for x in source_category_path if str(x).strip()]
        tgt = [str(x).strip() for x in takealot_path if str(x).strip()]
        if not src or not tgt:
            return False, "source_category_path 或 takealot_path 为空。"
        try:
            import yaml
            overrides_path = self._work_root / "input" / "category_overrides.yaml"
            overrides_path.parent.mkdir(parents=True, exist_ok=True)
            if overrides_path.exists():
                data = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    data = []
            else:
                data = []

            title = str((self._data.product_info or {}).get("title", "")).strip()
            keywords = [title, *src]
            keywords = [x for x in (str(k).strip() for k in keywords) if x]
            new_item = {
                "source_category_path": src,
                "keywords": keywords,
                "takealot_path": tgt,
            }
            merged: list[dict[str, Any]] = []
            replaced = False
            for item in data:
                if (
                    isinstance(item, dict)
                    and [str(x).strip() for x in item.get("source_category_path", []) if str(x).strip()] == src
                ):
                    merged.append(new_item)
                    replaced = True
                else:
                    merged.append(item)
            if not replaced:
                merged.append(new_item)
            overrides_path.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")
            return True, "ok"
        except Exception as e:
            return False, str(e)

    def _build_image_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(6)

        # 原图区
        src_header = QHBoxLayout()
        src_header.addWidget(QLabel("<b>原图（1688，勾选当副图用）</b>"))
        src_header.addStretch()
        self._translate_btn = QPushButton("翻译选中→副图")
        self._translate_btn.setFixedWidth(120)
        self._translate_btn.setToolTip("把选中的 1688 原图中文文字翻译成英文，生成后出现在下方 AI 图区供选用")
        self._translate_btn.setStyleSheet(
            "QPushButton { background: #1565C0; color: white; border-radius: 4px; padding: 4px 8px; }"
            "QPushButton:hover { background: #0D47A1; }"
            "QPushButton:disabled { background: #aaa; }"
        )
        self._translate_btn.clicked.connect(self._on_translate_src)
        src_header.addWidget(self._translate_btn)
        layout.addLayout(src_header)
        self._src_scroll = self._make_h_scroll_area()
        self._src_container = QWidget()
        self._src_layout = QHBoxLayout(self._src_container)
        self._src_layout.setSpacing(6)
        self._src_layout.setContentsMargins(4, 4, 4, 4)
        self._src_scroll.setWidget(self._src_container)
        layout.addWidget(self._src_scroll)

        self._src_placeholder = QLabel("原图加载中…")
        self._src_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._src_layout.addWidget(self._src_placeholder)

        # AI 生成图区
        gen_header = QHBoxLayout()
        gen_header.addWidget(QLabel("<b>AI 生成主图（白底，勾选要用的）</b>"))
        gen_header.addStretch()
        self._gen_status = QLabel("等待生成…")
        self._gen_status.setStyleSheet("color: #888;")
        gen_header.addWidget(self._gen_status)
        layout.addLayout(gen_header)

        self._img_progress = QProgressBar()
        self._img_progress.setRange(0, 0)
        self._img_progress.setFixedHeight(4)
        self._img_progress.setVisible(False)
        layout.addWidget(self._img_progress)

        self._gen_scroll = self._make_h_scroll_area(height=205)
        self._gen_container = QWidget()
        self._gen_layout = QHBoxLayout(self._gen_container)
        self._gen_layout.setSpacing(8)
        self._gen_layout.setContentsMargins(4, 4, 4, 4)
        self._gen_scroll.setWidget(self._gen_container)
        layout.addWidget(self._gen_scroll)

        self._gen_placeholder = QLabel("AI 正在生成白底图，请稍候…")
        self._gen_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gen_placeholder.setStyleSheet("color: #aaa;")
        self._gen_layout.addWidget(self._gen_placeholder)

        return panel

    def _build_fields_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(6)

        # ── 1688 原始数据展示区（折叠）─────────────────────────────────────────
        raw_info = self._data.product_info or {}
        attrs: dict = raw_info.get("product_attrs", {})
        title_zh = raw_info.get("title", "")
        price_text = raw_info.get("price_text", "")

        src_header = QHBoxLayout()
        src_lbl = QLabel("<b>📦 1688 原始数据（参考）</b>")
        src_lbl.setStyleSheet("color: #666;")
        src_header.addWidget(src_lbl)
        src_header.addStretch()
        self._src_toggle_btn = QPushButton("展开")
        self._src_toggle_btn.setFixedWidth(50)
        self._src_toggle_btn.setStyleSheet("QPushButton { border: none; color: #1976D2; }")
        src_header.addWidget(self._src_toggle_btn)
        layout.addLayout(src_header)

        self._src_info_panel = QWidget()
        src_panel_layout = QVBoxLayout(self._src_info_panel)
        src_panel_layout.setContentsMargins(4, 0, 4, 4)
        src_panel_layout.setSpacing(2)

        # 标题 + 价格
        if title_zh:
            lbl = QLabel(f"<b>标题：</b>{title_zh[:80]}")
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color: #444; font-size: 11px;")
            src_panel_layout.addWidget(lbl)
        if price_text:
            src_panel_layout.addWidget(QLabel(f"<b>价格：</b>{price_text}"))

        # 商品属性表
        if attrs:
            attr_scroll = QScrollArea()
            attr_scroll.setWidgetResizable(True)
            attr_scroll.setFixedHeight(130)
            attr_scroll.setFrameShape(QFrame.Shape.StyledPanel)
            attr_inner = QWidget()
            attr_grid = QGridLayout(attr_inner)
            attr_grid.setSpacing(2)
            attr_grid.setContentsMargins(4, 4, 4, 4)
            items = list(attrs.items())[:30]
            for idx, (k, v) in enumerate(items):
                row, col_base = divmod(idx, 2)
                key_lbl = QLabel(f"<span style='color:#888;'>{k}：</span>")
                key_lbl.setFixedWidth(100)
                val_lbl = QLabel(f"<b>{str(v)[:40]}</b>")
                val_lbl.setWordWrap(True)
                attr_grid.addWidget(key_lbl, row, col_base * 2)
                attr_grid.addWidget(val_lbl, row, col_base * 2 + 1)
            attr_inner.setLayout(attr_grid)
            attr_scroll.setWidget(attr_inner)
            src_panel_layout.addWidget(attr_scroll)
        else:
            src_panel_layout.addWidget(QLabel("<i>（本次未抓取到商品属性，重新运行可获取）</i>"))

        self._src_info_panel.setVisible(False)  # 默认折叠
        layout.addWidget(self._src_info_panel)
        self._src_toggle_btn.clicked.connect(self._toggle_src_panel)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #e0e0e0;")
        layout.addWidget(sep)

        # ── Takealot 字段区（可编辑）─────────────────────────────────────────
        layout.addWidget(QLabel("<b>产品信息（由 AI 生成后可编辑）</b>"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._fields_layout = QVBoxLayout(inner)
        self._fields_layout.setSpacing(8)
        self._fields_layout.setContentsMargins(4, 4, 4, 4)
        scroll.setWidget(inner)
        layout.addWidget(scroll)

        self._field_widgets: dict[str, QComboBox | QLineEdit | QTextEdit] = {}
        self._build_field_rows()

        return panel

    def _build_field_rows(self):
        """构建标题/副标题/卖点/清单 + portal 字段输入框。"""
        fl = self._fields_layout

        fl.addWidget(QLabel("<b>标题 *</b>"))
        self._title_edit = QLineEdit(self._data.title)
        self._title_edit.setMaxLength(150)
        self._title_edit.setPlaceholderText("点击「AI 生成」后自动填入（英文，≤75字符）")
        fl.addWidget(self._title_edit)

        fl.addWidget(QLabel("副标题"))
        self._subtitle_edit = QLineEdit(self._data.subtitle)
        self._subtitle_edit.setPlaceholderText("点击「AI 生成」后自动填入（英文，≤110字符）")
        fl.addWidget(self._subtitle_edit)

        fl.addWidget(QLabel("<b>Key Selling Features *</b>"))
        self._key_features_edit = QTextEdit()
        self._key_features_edit.setFixedHeight(120)
        pre_kf = (self._data.field_values.get("Key Selling Features")
                  or self._data.field_values.get("key_features", ""))
        self._key_features_edit.setPlainText(pre_kf)
        self._key_features_edit.setPlaceholderText("- Feature 1\n- Feature 2\n- Feature 3\n- Feature 4")
        fl.addWidget(self._key_features_edit)
        self._field_widgets["Key Selling Features"] = self._key_features_edit

        fl.addWidget(QLabel("<b>What's in the Box *</b>"))
        self._whats_in_box_edit = QTextEdit()
        self._whats_in_box_edit.setFixedHeight(80)
        pre_wib = (self._data.field_values.get("What's in the Box")
                   or self._data.field_values.get("whats_in_box", ""))
        self._whats_in_box_edit.setPlainText(pre_wib)
        self._whats_in_box_edit.setPlaceholderText("1 x Product Name\n1 x Charging Cable\n1 x User Manual")
        fl.addWidget(self._whats_in_box_edit)
        self._field_widgets["What's in the Box"] = self._whats_in_box_edit

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #e0e0e0;")
        fl.addWidget(sep)

        # 顶部已有标题/副标题/卖点/清单，portal 里同名字段跳过避免重复
        _SKIP_LABELS = {
            "product title", "product subtitle",
            "key selling features", "what's in the box",
            "video link (url)",
        }

        if not self._data.portal_fields:
            fl.addWidget(QLabel("<i>（未探测到 portal 字段）</i>"))
        else:
            required = [f for f in self._data.portal_fields
                        if f.get("required") and f.get("label", "").lower() not in _SKIP_LABELS]
            optional = [f for f in self._data.portal_fields
                        if not f.get("required") and f.get("label", "").lower() not in _SKIP_LABELS]

            if required:
                req_lbl = QLabel("<b>必填字段</b>")
                req_lbl.setStyleSheet("color: #d32f2f;")
                fl.addWidget(req_lbl)
                for fdef in required:
                    self._add_field_row(fdef, required=True)

            if optional:
                opt_lbl = QLabel("<b>选填字段</b>")
                opt_lbl.setStyleSheet("color: #555; margin-top: 8px;")
                fl.addWidget(opt_lbl)
                for fdef in optional:
                    self._add_field_row(fdef, required=False)

        fl.addStretch()

    def _add_field_row(self, fdef: dict, required: bool):
        label = fdef.get("label", "")
        key   = fdef.get("key", "") or label
        hint  = fdef.get("placeholder", "") or fdef.get("hint", "")
        ftype = fdef.get("type", "text")
        options: list[str] = fdef.get("options", []) or []
        pre_val = self._data.field_values.get(key, "") or self._data.field_values.get(label, "")

        star = " *" if required else ""
        row_lbl = QLabel(f"{label}{star}")
        if required:
            row_lbl.setStyleSheet("font-weight: bold;")
        if hint:
            row_lbl.setToolTip(hint)
        self._fields_layout.addWidget(row_lbl)

        if options or ftype in ("combobox", "select"):
            # 下拉框字段（有 options 时加选项，没有也允许手动输入）
            w: QComboBox | QLineEdit | QTextEdit = QComboBox()
            w.addItem("")   # 空选项（未选）
            for opt in options:
                w.addItem(str(opt))
            # 预填
            if pre_val:
                idx = w.findText(pre_val, Qt.MatchFlag.MatchFixedString)
                if idx >= 0:
                    w.setCurrentIndex(idx)
                else:
                    w.setCurrentText(pre_val)
            w.setEditable(True)   # 允许手动输入
        elif ftype == "textarea":
            w = QTextEdit()
            w.setFixedHeight(60)
            w.setPlainText(pre_val)
            w.setPlaceholderText(hint or label)
        else:
            w = QLineEdit(pre_val)
            w.setPlaceholderText(hint or label)

        # 用 key 作为主键，但也记录 label 映射
        self._field_widgets[key] = w
        if label and label != key:
            self._field_widgets[label] = w   # 双向映射
        self._fields_layout.addWidget(w)

    def _build_bottom_bar(self) -> QWidget:
        bar = QWidget()
        root = QVBoxLayout(bar)
        root.setSpacing(6)
        root.setContentsMargins(0, 4, 0, 0)

        # ── 行1：AI 文字生成 ──────────────────────────────────────────────────
        txt_row = QHBoxLayout()
        txt_row.setSpacing(8)

        ai_icon = QLabel("🤖")
        txt_row.addWidget(ai_icon)

        self._txt_instruction = QLineEdit()
        self._txt_instruction.setPlaceholderText(
            "可直接点「AI 生成」一键生成全部内容，或在此输入额外要求，如：强调无线连接和兼容性，简洁专业风格"
        )
        self._txt_instruction.returnPressed.connect(self._on_generate_text)
        txt_row.addWidget(self._txt_instruction, stretch=1)

        self._txt_gen_btn = QPushButton("AI 生成")
        self._txt_gen_btn.setFixedWidth(120)
        self._txt_gen_btn.setStyleSheet(
            "QPushButton { background: #7B1FA2; color: white; border-radius: 4px; padding: 6px; }"
            "QPushButton:hover { background: #6A1B9A; }"
            "QPushButton:disabled { background: #aaa; }"
        )
        self._txt_gen_btn.clicked.connect(self._on_generate_text)
        txt_row.addWidget(self._txt_gen_btn)

        self._txt_status = QLabel("等待生成…")
        self._txt_status.setStyleSheet("color: #888; min-width: 120px;")
        txt_row.addWidget(self._txt_status)

        root.addLayout(txt_row)

        self._txt_progress = QProgressBar()
        self._txt_progress.setRange(0, 0)
        self._txt_progress.setFixedHeight(3)
        self._txt_progress.setVisible(False)
        root.addWidget(self._txt_progress)

        # 调试面板：显示豆包原始输出（折叠）
        dbg_header = QHBoxLayout()
        dbg_lbl = QLabel("🔍 AI 原始输出（调试）")
        dbg_lbl.setStyleSheet("color: #999; font-size: 11px;")
        dbg_header.addWidget(dbg_lbl)
        dbg_header.addStretch()
        self._dbg_toggle_btn = QPushButton("展开")
        self._dbg_toggle_btn.setFixedWidth(50)
        self._dbg_toggle_btn.setStyleSheet("QPushButton { border: none; color: #999; font-size: 11px; }")
        self._dbg_toggle_btn.clicked.connect(self._toggle_dbg_panel)
        dbg_header.addWidget(self._dbg_toggle_btn)
        root.addLayout(dbg_header)

        self._dbg_panel = QPlainTextEdit()
        self._dbg_panel.setReadOnly(True)
        self._dbg_panel.setFixedHeight(120)
        self._dbg_panel.setStyleSheet("font-size: 10px; color: #555; background: #f8f8f8;")
        self._dbg_panel.setPlaceholderText("点击「AI 生成」后，这里会显示返回的原始 JSON 和 Prompt...")
        self._dbg_panel.setVisible(False)
        root.addWidget(self._dbg_panel)

        # ── 行2：图片修改 + 操作按钮 ─────────────────────────────────────────
        img_row = QHBoxLayout()
        img_row.setSpacing(8)

        img_icon = QLabel("🎨")
        img_row.addWidget(img_icon)

        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText(
            "图片修改要求（可选）：如 背景再白一点 / 去掉阴影…"
        )
        self._chat_input.returnPressed.connect(self._on_refine_image)
        img_row.addWidget(self._chat_input, stretch=1)

        img_row.addWidget(QLabel("张数:"))
        self._img_count_spin = QSpinBox()
        self._img_count_spin.setRange(1, 8)
        # 默认生成 4 张（1 张主图 + 3 张副图），减少等待时间和费用
        self._img_count_spin.setValue(4)
        self._img_count_spin.setFixedWidth(52)
        self._img_count_spin.setToolTip("生成图片数量（1张主图 + 4张场景/卖点副图）")
        img_row.addWidget(self._img_count_spin)

        self._refine_btn = QPushButton("AI 生成图片")
        self._refine_btn.setFixedWidth(110)
        self._refine_btn.clicked.connect(self._on_refine_image)
        img_row.addWidget(self._refine_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #ddd;")
        img_row.addWidget(sep)

        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedWidth(80)
        cancel_btn.clicked.connect(self.reject)
        img_row.addWidget(cancel_btn)

        self._confirm_btn = QPushButton("✅ 确认提交")
        self._confirm_btn.setFixedWidth(110)
        self._confirm_btn.setStyleSheet(
            "QPushButton { background: #1976D2; color: white; border-radius: 4px; padding: 6px; }"
            "QPushButton:hover { background: #1565C0; }"
        )
        self._confirm_btn.clicked.connect(self._on_confirm)
        img_row.addWidget(self._confirm_btn)

        root.addLayout(img_row)
        return bar

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _make_h_scroll_area(height: int = 120) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedHeight(height)
        scroll.setFrameShape(QFrame.Shape.StyledPanel)
        return scroll

    def _load_source_thumbnails(self, pairs: list):
        """pairs: list of (thumb_bytes, full_bytes) 或旧格式 list[bytes]（兼容）"""
        while self._src_layout.count():
            item = self._src_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._src_image_cards.clear()

        if not pairs:
            self._src_layout.addWidget(QLabel("（无原图）"))
            return

        for i, item in enumerate(pairs):
            if isinstance(item, (tuple, list)) and len(item) == 2:
                thumb_bytes, full_bytes = item
            else:
                thumb_bytes = full_bytes = item  # 旧格式兼容

            card = _ImageCard(thumb_bytes, i, full_bytes=full_bytes)
            self._src_layout.addWidget(card)
            self._src_image_cards.append(card)
        self._src_layout.addStretch()

    def _show_generated_images(self, images: list[bytes], append: bool = False):
        """显示 AI 生成图片。append=True 时追加到现有图片后面，不清空。"""
        if not append:
            # 移除 stretch（如有）以及空占位
            while self._gen_layout.count():
                item = self._gen_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self._generated_cards.clear()

        if not images:
            if not self._generated_cards:
                self._gen_layout.addWidget(QLabel("生成失败，请重试"))
            return

        start_idx = len(self._generated_cards)
        for i, img_bytes in enumerate(images):
            card = _ImageCard(img_bytes, start_idx + i,
                              reorder_callback=self._reorder_generated_card)
            if start_idx == 0 and i == 0:
                card.set_selected(True)   # 第一张自动选中（仅初次）
            self._gen_layout.addWidget(card)
            self._generated_cards.append(card)
        self._gen_layout.addStretch()
        self._gen_status.setText(f"已生成 {len(self._generated_cards)} 张，勾选要使用的")

    def _reorder_generated_card(self, src_obj_id: int, tgt_obj_id: int) -> None:
        """拖拽放置回调：把 src 卡片移到 tgt 卡片位置，重建布局。"""
        src = next((c for c in self._generated_cards if id(c) == src_obj_id), None)
        tgt = next((c for c in self._generated_cards if id(c) == tgt_obj_id), None)
        if src is None or tgt is None or src is tgt:
            return
        src_idx = self._generated_cards.index(src)
        tgt_idx = self._generated_cards.index(tgt)
        # 从旧位置取出，插入目标位置
        self._generated_cards.pop(src_idx)
        new_tgt_idx = self._generated_cards.index(tgt)
        self._generated_cards.insert(new_tgt_idx, src)
        # 清空布局（不销毁 widget）并按新顺序重新加入
        while self._gen_layout.count():
            self._gen_layout.takeAt(0)
        for card in self._generated_cards:
            self._gen_layout.addWidget(card)
            card.show()
        self._gen_layout.addStretch()
        self._schedule_autosave()

    # ── AI 图片生成逻辑 ───────────────────────────────────────────────────────

    def _start_image_generate(self, is_first: bool = True, instruction: str = ""):
        if self._img_worker_thread and self._img_worker_thread.isRunning():
            return

        # 收集用户选中的原图 URL（用于图生图参考）
        selected_src_urls: list[str] = []
        session_urls: list[str] = self._session.source_urls if self._session else []
        for i, card in enumerate(self._src_image_cards):
            if card.is_selected() and i < len(session_urls):
                selected_src_urls.append(session_urls[i])
        # 未选任何原图时，使用全部原图中的第一张
        ref_urls = selected_src_urls if selected_src_urls else (session_urls[:1] if session_urls else [])

        count = self._img_count_spin.value()

        self._refine_btn.setEnabled(False)
        self._img_progress.setVisible(True)
        self._gen_status.setText(f"AI 生成中（参考 {len(ref_urls)} 张原图）…" if ref_urls else "AI 生成中…")

        self._img_worker_thread = QThread()
        self._img_worker = _ImageWorker(self._session, instruction, is_first, reference_urls=ref_urls, count=count)
        self._img_worker.moveToThread(self._img_worker_thread)

        self._img_worker_thread.started.connect(self._img_worker.run)
        self._img_worker.finished.connect(self._on_img_done)
        self._img_worker.error.connect(self._on_img_error)
        self._img_worker.finished.connect(self._img_worker_thread.quit)
        self._img_worker.error.connect(self._img_worker_thread.quit)

        self._img_worker_thread.start()

    def _load_source_async(self):
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from .image_generator import _download_bytes, _bytes_to_thumbnail

            urls = (self._session.source_urls if self._session else [])[:8]

            def _fetch_one(url: str):
                full = _download_bytes(url)
                if full:
                    return (_bytes_to_thumbnail(full, 100), full)
                return None

            pairs: list[tuple[bytes, bytes]] = []
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = {pool.submit(_fetch_one, u): u for u in urls}
                for fut in as_completed(futs):
                    result = fut.result()
                    if result:
                        pairs.append(result)
            self._src_thumbs_ready.emit(pairs)
        except Exception:
            pass

    def _on_img_done(self, images: list[bytes]):
        self._img_progress.setVisible(False)
        self._refine_btn.setEnabled(True)
        self._show_generated_images(images, append=True)

    def _on_img_error(self, msg: str):
        self._img_progress.setVisible(False)
        self._refine_btn.setEnabled(True)
        short = msg[:100] if len(msg) > 100 else msg
        self._gen_status.setText(f"生成失败：{short}")
        # 在图片区也显示错误信息
        while self._gen_layout.count():
            item = self._gen_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        err_lbl = QLabel(f"❌ 生成失败：\n{msg[:300]}")
        err_lbl.setWordWrap(True)
        err_lbl.setStyleSheet("color: #c62828; padding: 8px;")
        self._gen_layout.addWidget(err_lbl)

    def _on_refine_image(self):
        if not self._session:
            return
        instruction = self._chat_input.text().strip()
        self._start_image_generate(is_first=False, instruction=instruction)

    def _on_translate_src(self):
        """把选中的 1688 原图逐张翻译中文→英文，追加到 AI 图区。"""
        if self._translate_thread and self._translate_thread.isRunning():
            return

        # 收集选中原图的 full_bytes
        selected_bytes = [c.get_full_bytes() for c in self._src_image_cards if c.is_selected()]
        if not selected_bytes:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "提示", "请先在原图区勾选要翻译的图片")
            return

        self._translate_btn.setEnabled(False)
        self._gen_status.setText(f"翻译中（共 {len(selected_bytes)} 张）…")

        self._translate_thread = QThread()
        self._translate_worker = _TranslateWorker(selected_bytes)   # 保存引用，防 GC
        self._translate_worker.moveToThread(self._translate_thread)

        self._translate_thread.started.connect(self._translate_worker.run)
        self._translate_worker.partial.connect(self._on_translate_partial)
        self._translate_worker.finished.connect(self._on_translate_done)
        self._translate_worker.error.connect(self._on_translate_error)
        self._translate_worker.finished.connect(self._translate_thread.quit)
        self._translate_worker.error.connect(self._translate_thread.quit)
        self._translate_thread.finished.connect(lambda: self._translate_btn.setEnabled(True))

        self._translate_thread.start()

    def _on_translate_partial(self, images: list[bytes]):
        """每完成一张翻译图立刻追加显示。"""
        self._show_generated_images(images, append=True)

    def _on_translate_done(self, images: list[bytes]):
        self._gen_status.setText(f"翻译完成，共 {len(self._generated_cards)} 张")

    def _on_translate_error(self, msg: str):
        self._gen_status.setText(f"翻译失败：{msg[:80]}")
        self._translate_btn.setEnabled(True)



    def _on_generate_text(self):
        """用户点击「AI 生成」按钮。"""
        if self._txt_worker_thread and self._txt_worker_thread.isRunning():
            return

        instructions = self._txt_instruction.text().strip()
        self._txt_gen_btn.setEnabled(False)
        self._txt_progress.setVisible(True)
        self._txt_status.setText("AI 生成中…")

        self._txt_worker_thread = QThread()
        self._txt_worker = _ListingWorker(
            product_info=self._data.product_info,
            portal_fields=self._data.portal_fields,
            user_instructions=instructions,
        )
        self._txt_worker.moveToThread(self._txt_worker_thread)

        self._txt_worker_thread.started.connect(self._txt_worker.run)
        self._txt_worker.finished.connect(self._on_text_gen_done)
        self._txt_worker.error.connect(self._on_text_gen_error)
        self._txt_worker.finished.connect(self._txt_worker_thread.quit)
        self._txt_worker.error.connect(self._txt_worker_thread.quit)

        self._txt_worker_thread.start()

    def _on_text_gen_done(self, result: dict):
        self._txt_progress.setVisible(False)
        self._txt_gen_btn.setEnabled(True)

        # 填充调试面板
        raw = result.pop("_debug_raw", "") or ""
        prompt = result.pop("_debug_prompt", "") or ""
        if raw or prompt:
            debug_text = f"=== AI 返回 ===\n{raw}\n\n=== 发送的 Prompt ===\n{prompt}"
            self._dbg_panel.setPlainText(debug_text)
            self._dbg_panel.setVisible(True)
            self._dbg_toggle_btn.setText("折叠")

        title = str(result.get("title", "")).strip()
        subtitle = str(result.get("subtitle", "")).strip()

        if title:
            self._title_edit.setText(title[:75])
        if subtitle:
            self._subtitle_edit.setText(subtitle[:110])

        # 填充 portal 字段值
        field_values: dict = result.get("field_values", {}) or {}

        # 把顶层 key_features / whats_in_box 也合并到 field_values，对应 portal 字段名
        key_features = str(result.get("key_features", "")).strip()
        whats_in_box = result.get("whats_in_box", [])
        if isinstance(whats_in_box, list):
            whats_in_box_str = "\n".join(str(x) for x in whats_in_box if str(x).strip())
        else:
            whats_in_box_str = str(whats_in_box).strip()

        # 用常见 portal label 作为候选键，把顶层值注入 field_values
        if key_features:
            for kf_label in ("Key Selling Features", "key_features", "Key Features"):
                if kf_label not in field_values:
                    field_values[kf_label] = key_features
        if whats_in_box_str:
            for wib_label in ("What's in the Box", "whats_in_box", "What's In The Box"):
                if wib_label not in field_values:
                    field_values[wib_label] = whats_in_box_str

        filled = 0
        for k, v in field_values.items():
            v_str = str(v).strip()
            if not v_str:
                continue
            # 尝试 key 匹配，再尝试 label 匹配
            widget = self._field_widgets.get(k)
            if widget is None:
                # 模糊匹配：忽略大小写
                k_lower = k.lower()
                for wk, wv in self._field_widgets.items():
                    if wk.lower() == k_lower:
                        widget = wv
                        break
            if widget is not None:
                if isinstance(widget, QComboBox):
                    # 精确匹配
                    idx = widget.findText(v_str, Qt.MatchFlag.MatchFixedString)
                    if idx < 0:
                        # 大小写不敏感匹配
                        idx = widget.findText(v_str, Qt.MatchFlag.MatchFixedString | Qt.MatchFlag.MatchCaseSensitive ^ Qt.MatchFlag.MatchCaseSensitive)
                    if idx < 0:
                        # 逐项检查（case-insensitive fallback）
                        v_lower = v_str.lower()
                        for i in range(widget.count()):
                            if widget.itemText(i).lower() == v_lower:
                                idx = i
                                break
                    if idx >= 0:
                        widget.setCurrentIndex(idx)
                    # 不在选项里时保持原值，不写入非法值
                elif isinstance(widget, QTextEdit):
                    widget.setPlainText(v_str)
                else:
                    widget.setText(v_str)
                filled += 1

        self._txt_status.setText(f"✅ 已生成，填入 {filled} 个字段")

    def _on_text_gen_error(self, msg: str):
        self._txt_progress.setVisible(False)
        self._txt_gen_btn.setEnabled(True)
        self._txt_status.setText(f"❌ 生成失败：{msg[:60]}")
        QMessageBox.warning(self, "AI 生成失败", f"返回错误：\n{msg[:300]}")

    # ── 确认提交 ──────────────────────────────────────────────────────────────

    def _toggle_dbg_panel(self):
        visible = self._dbg_panel.isVisible()
        self._dbg_panel.setVisible(not visible)
        self._dbg_toggle_btn.setText("折叠" if not visible else "展开")

    def _toggle_src_panel(self):
        visible = self._src_info_panel.isVisible()
        self._src_info_panel.setVisible(not visible)
        self._src_toggle_btn.setText("折叠" if not visible else "展开")

    def _prefill_from_product_attrs(self):
        """
        用 1688 商品属性预填 portal 字段中已知的对应项。
        仅在字段当前为空时填入，不覆盖用户已有内容。
        """
        attrs: dict = (self._data.product_info or {}).get("product_attrs", {})
        if not attrs:
            return

        # 中文属性名 → 英文 portal 字段 label / key 的映射（常见对应关系）
        _ATTR_MAP = {
            "型号": ["Model Number", "model_number", "model"],
            "货号": ["Model Number", "model_number", "model"],
            "品牌": ["Brand", "brand"],
            "颜色": ["Colour", "Color", "colour", "color"],
            "颜色分类": ["Colour", "Color", "colour", "color"],
            "材质": ["Material", "material"],
            "重量": ["Packaged Weight (g)", "weight", "packaged_weight"],
            "外观尺寸": ["Packaged Length (cm)", "dimensions", "size"],
            "操作系统": ["Operating System", "os", "operating_system"],
            "屏幕尺寸": ["Screen Size", "screen_size", "display_size"],
            "分辨率": ["Resolution", "resolution"],
            "运行内存": ["RAM", "Memory", "ram", "memory"],
            "接口": ["Connectivity", "Interface", "interface"],
            "适合车型": ["Compatible Vehicle", "compatibility"],
            "CPU类型": ["Processor", "CPU", "processor"],
        }

        for zh_key, en_keys in _ATTR_MAP.items():
            # 从 attrs 里找中文键（精确 or 部分匹配）
            val = attrs.get(zh_key, "")
            if not val:
                for ak, av in attrs.items():
                    if zh_key in ak:
                        val = av
                        break
            if not val:
                continue

            # 对重量字段做解析（"单218g+带盒385g" → "385"）
            if zh_key in ("重量", "净重", "毛重"):
                try:
                    from .csv_exporter import _parse_weight_g
                    val = _parse_weight_g(str(val))
                except Exception:
                    pass

            # 对尺寸字段做解析（"20*10*5cm" → 分别填 H/L/W）
            if zh_key == "外观尺寸":
                import re as _re
                dim_parts = _re.findall(r"\d+(?:\.\d+)?", str(val))
                if len(dim_parts) >= 3:
                    # 分别填 Length / Width / Height
                    for label, part in [
                        ("Packaged Length (cm)", dim_parts[0]),
                        ("Packaged Width (cm)",  dim_parts[1]),
                        ("Packaged Height (cm)", dim_parts[2]),
                    ]:
                        w = self._field_widgets.get(label)
                        if w is None:
                            for wk, wv in self._field_widgets.items():
                                if wk.lower() == label.lower():
                                    w = wv
                                    break
                        if w and not getattr(w, "text", lambda: "")().strip():
                            w.setText(part)
                    continue  # 不走通用逻辑

            # 找对应的 portal widget
            for en_key in en_keys:
                widget = self._field_widgets.get(en_key)
                if widget is None:
                    # 忽略大小写匹配
                    for wk, wv in self._field_widgets.items():
                        if wk.lower() == en_key.lower():
                            widget = wv
                            break
                if widget is not None:
                    # 只有当字段当前为空时才填
                    v_str = str(val).strip()
                    if isinstance(widget, QComboBox):
                        if not widget.currentText().strip():
                            idx = widget.findText(v_str, Qt.MatchFlag.MatchFixedString)
                            if idx < 0:
                                v_lower = v_str.lower()
                                for i in range(widget.count()):
                                    if widget.itemText(i).lower() == v_lower:
                                        idx = i
                                        break
                            if idx >= 0:
                                widget.setCurrentIndex(idx)
                            # 不在选项里时保持原值，不写入非法值
                    elif isinstance(widget, QTextEdit):
                        if not widget.toPlainText().strip():
                            widget.setPlainText(v_str)
                    else:
                        if not widget.text().strip():
                            widget.setText(v_str)
                    break  # 找到一个就停止

    def _on_confirm(self):
        title = self._title_edit.text().strip()
        if not title:
            QMessageBox.warning(self, "提示", "标题不能为空，请先点「AI 生成」或手动填写")
            return

        # 收集字段值（去重，key 优先）
        field_values: dict[str, str] = {}
        seen_widgets = set()
        for label, w in self._field_widgets.items():
            wid = id(w)
            if wid in seen_widgets:
                continue
            seen_widgets.add(wid)
            if isinstance(w, QComboBox):
                val = w.currentText().strip()
            elif isinstance(w, QTextEdit):
                val = w.toPlainText().strip()
            else:
                val = w.text().strip()
            if val:
                field_values[label] = val

        # portal 中如有 Product Title / Product Subtitle 字段，用顶部输入值覆盖
        portal_labels = {f.get("label", "") for f in self._data.portal_fields}
        if "Product Title" in portal_labels:
            field_values["Product Title"] = title
        if "Product Subtitle" in portal_labels:
            sub = self._subtitle_edit.text().strip()
            if sub:
                field_values["Product Subtitle"] = sub

        # 收集勾选的 AI 生成图（使用 full_bytes）
        ai_selected = [c.get_full_bytes() for c in self._generated_cards if c.is_selected()]
        # 收集勾选的原图（使用 full_bytes）
        src_selected = [c.get_full_bytes() for c in self._src_image_cards if c.is_selected()]
        selected = ai_selected + src_selected

        self._result = PreviewResult(
            title=title,
            subtitle=self._subtitle_edit.text().strip(),
            selected_image_bytes=selected,
            field_values=field_values,
            category_path=[str(x).strip() for x in (self._data.category_path or []) if str(x).strip()],
            portal_fields=list(self._data.portal_fields or []),
            confirmed=True,
        )
        self.accept()

    def closeEvent(self, event):
        """窗口 ✕ 关闭时自动保存。"""
        self.autosave()
        super().closeEvent(event)

    def reject(self) -> None:
        """取消按钮 / Esc 关闭时自动保存（reject 不触发 closeEvent，需单独处理）。"""
        self.autosave()
        super().reject()

    # ── 公开方法 ──────────────────────────────────────────────────────────────

    def get_result(self) -> PreviewResult:
        return self._result

    # ── 自动保存 / 恢复 ───────────────────────────────────────────────────────

    def _schedule_autosave(self) -> None:
        """任何字段变动时调用，重置防抖定时器（1.5 秒无变动后写盘）。"""
        if self._run_dir:
            self._autosave_timer.start()

    def _connect_autosave_signals(self) -> None:
        """把标题、副标题、所有 portal 字段的变动信号连接到防抖保存。"""
        if hasattr(self, "_title_edit"):
            self._title_edit.textChanged.connect(self._schedule_autosave)
        if hasattr(self, "_subtitle_edit"):
            self._subtitle_edit.textChanged.connect(self._schedule_autosave)
        if hasattr(self, "_key_features_edit"):
            self._key_features_edit.textChanged.connect(self._schedule_autosave)
        if hasattr(self, "_whats_in_box_edit"):
            self._whats_in_box_edit.textChanged.connect(self._schedule_autosave)
        for w in getattr(self, "_field_widgets", {}).values():
            if isinstance(w, QComboBox):
                w.currentTextChanged.connect(self._schedule_autosave)
            elif hasattr(w, "textChanged"):
                w.textChanged.connect(self._schedule_autosave)

    def _autosave_path(self):
        return self._run_dir / "preview_autosave.json" if self._run_dir else None

    def autosave(self) -> None:
        """把当前填写的字段值保存到 run_dir/preview_autosave.json。"""
        p = self._autosave_path()
        if not p:
            return
        try:
            import json as _json
            data: dict = {}
            title = self._title_edit.text().strip() if hasattr(self, "_title_edit") else ""
            subtitle = self._subtitle_edit.text().strip() if hasattr(self, "_subtitle_edit") else ""
            cat_text = self._takealot_category_edit.text().strip() if hasattr(self, "_takealot_category_edit") else ""
            if title:
                data["title"] = title
            if subtitle:
                data["subtitle"] = subtitle
            if cat_text:
                data["takealot_category_text"] = cat_text
            if self._data.category_path:
                data["takealot_category_path"] = [str(x).strip() for x in self._data.category_path if str(x).strip()]
            field_values: dict = {}
            for label, w in getattr(self, "_field_widgets", {}).items():
                if isinstance(w, QComboBox):
                    val = w.currentText().strip()
                elif hasattr(w, "text"):
                    val = w.text().strip()
                elif hasattr(w, "toPlainText"):
                    val = w.toPlainText().strip()
                else:
                    val = ""
                if val:
                    field_values[label] = val
            data["field_values"] = field_values
            p.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _restore_autosave(self) -> None:
        """若 run_dir 下存在 preview_autosave.json，用其中的值覆盖当前字段。"""
        p = self._autosave_path()
        if not p or not p.exists():
            return
        try:
            import json as _json
            data = _json.loads(p.read_text(encoding="utf-8"))
            if data.get("title") and hasattr(self, "_title_edit"):
                self._title_edit.setText(data["title"])
            if data.get("subtitle") and hasattr(self, "_subtitle_edit"):
                self._subtitle_edit.setText(data["subtitle"])
            cat_text = data.get("takealot_category_text") or data.get("category_text")
            if cat_text and hasattr(self, "_takealot_category_edit"):
                self._takealot_category_edit.setText(str(cat_text))
            if isinstance(data.get("takealot_category_path"), list):
                self._data.category_path = [str(x).strip() for x in data["takealot_category_path"] if str(x).strip()]
                if hasattr(self, "_takealot_category_edit") and self._data.category_path:
                    self._takealot_category_edit.setText(self._fmt_path(self._data.category_path))
                self._refresh_takealot_category_label()
            for label, val in (data.get("field_values") or {}).items():
                w = getattr(self, "_field_widgets", {}).get(label)
                if w is None:
                    continue
                if isinstance(w, QComboBox):
                    idx = w.findText(val)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                elif hasattr(w, "setText"):
                    w.setText(val)
                elif hasattr(w, "setPlainText"):
                    w.setPlainText(val)
        except Exception:
            pass
