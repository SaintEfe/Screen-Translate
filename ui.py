"""Kontrol paneli ve çeviri penceresi.

Tüm QWidget işlemleri GUI thread'inde yapılır. Worker thread yalnızca
Qt sinyal köprüsü üzerinden sonuç/ durum bildirir.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, QRect, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QScreen,
    QShortcut,
    QTextOption,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from engine import POLL_DEFAULT, POLL_MAX, POLL_MIN, Roi, TranslationWorker
from roi import (
    FRAME_OPACITY_DEFAULT,
    FRAME_OPACITY_MAX,
    FRAME_OPACITY_MIN,
    RoiFrame,
    RoiSelectionController,
    exclude_from_capture,
)

SETTINGS_ORG = "SaintEfe"
SETTINGS_APP = "ScreenTranslate"
OPACITY_KEY = "frame/opacity"
APP_NAME = "Screen Translate"
APP_AUTHOR = "SaintEfe"

STYLE = """
QMainWindow, QWidget#panel { background: #12161d; }
QLabel { color: #dbe6f2; font-size: 12px; }
QLabel#title { color: #ffffff; font-size: 14px; font-weight: 600; }
QLabel#hint { color: #8fa3b8; font-size: 11px; }
QLabel#status { color: #9fd0ff; font-size: 11px; }
QLabel#error { color: #ff9a9a; font-size: 11px; }
QPushButton {
    background: #1f2937; color: #e8f1fb; border: 1px solid #2f3d4f;
    border-radius: 6px; padding: 6px 12px; font-size: 12px;
}
QPushButton:hover { background: #273445; }
QPushButton:disabled { color: #6b7c8f; background: #171d26; }
QPushButton#primary { background: #1d6fb8; border-color: #2b8ada; }
QPushButton#primary:hover { background: #2480cf; }
QComboBox {
    background: #1f2937; color: #e8f1fb; border: 1px solid #2f3d4f;
    border-radius: 6px; padding: 4px 8px; font-size: 12px;
}
QTextEdit#fallback {
    background: #0e131a; color: #e6eef8; border: 1px solid #26313f;
    border-radius: 6px; font-size: 12px;
}
QSlider::groove:horizontal {
    height: 4px; background: #26313f; border-radius: 2px;
}
QSlider::sub-page:horizontal { background: #1d6fb8; border-radius: 2px; }
QSlider::handle:horizontal {
    background: #2b8ada; width: 12px; margin: -5px 0; border-radius: 6px;
}
QSlider::handle:horizontal:hover { background: #3a9ae8; }
"""


class WorkerBridge(QObject):
    """Worker thread geri çağırmalarını Qt sinyallerine çevirir."""

    resultReady = Signal(int, str, bool)
    statusReady = Signal(str)
    errorReady = Signal(str)


class TranslationOverlay(QWidget):
    """ROI'ye bitişik, yarı saydam, her zaman üstte çeviri penceresi."""

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._view = QTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setFrameShape(QFrame.NoFrame)
        self._view.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._view.setStyleSheet(
            "background: transparent; color: #eaf2fb; border: none; font-size: 13px;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addWidget(self._view)
        self.resize(420, 220)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(16, 20, 28, 225))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 10, 10)

    def set_translation(self, text: str, truncated: bool = False) -> None:
        """Düz metin olarak yazar; HTML yorumlanmaz."""
        body = text
        if truncated:
            body = f"{text}\n\n… (metin 4000 karakterde kısaltıldı)"
        self._view.setPlainText(body)

    def place_near(self, capture: QRect, screen: Optional[QScreen]) -> bool:
        """ROI ile çakışmayan bir konum bulur; bulamazsa gizler."""
        target = screen or QGuiApplication.primaryScreen()
        if target is None:
            self.hide()
            return False
        avail = target.availableGeometry()
        width = max(220, min(420, avail.width()))
        height = max(120, min(240, avail.height() // 3))
        gap = 8
        candidates = [
            QRect(capture.left(), capture.bottom() + gap, width, height),
            QRect(capture.left(), capture.top() - gap - height, width, height),
            QRect(capture.right() + gap, capture.top(), width, height),
            QRect(capture.left() - gap - width, capture.top(), width, height),
        ]
        for candidate in candidates:
            placed = self._clamp(candidate, avail)
            if placed.intersects(capture):
                continue
            self.setGeometry(placed)
            self.show()
            self.raise_()
            return True
        self.hide()
        return False

    @staticmethod
    def _clamp(rect: QRect, avail: QRect) -> QRect:
        width = min(rect.width(), avail.width())
        height = min(rect.height(), avail.height())
        x = max(avail.left(), min(rect.left(), avail.right() - width + 1))
        y = max(avail.top(), min(rect.top(), avail.bottom() - height + 1))
        return QRect(x, y, width, height)


class ControlPanel(QMainWindow):
    """Komutları, durumu ve yedek çeviri görünümünü barındıran panel."""

    def __init__(self, ocr_command: Optional[str] = None) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · {APP_AUTHOR}")
        self.setObjectName("panel")
        self.setStyleSheet(STYLE)
        self.resize(380, 400)

        self._closing = False
        self._running = False
        self._resume_after_selection = False
        self._capture_rect = QRect()
        self._screen: Optional[QScreen] = None
        self._join_attempts = 0
        self._panel_excluded = False
        self._fallback_active = False
        self._roi_active = False
        self._shortcuts: list[QShortcut] = []
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        self._bridge = WorkerBridge()
        self._worker = TranslationWorker(
            self._bridge.resultReady.emit,
            self._bridge.statusReady.emit,
            self._bridge.errorReady.emit,
            poll_interval=POLL_DEFAULT,
            ocr_command=ocr_command,
        )
        self._bridge.resultReady.connect(self._on_result)
        self._bridge.statusReady.connect(self._set_status)
        self._bridge.errorReady.connect(self._on_error)

        self._overlay = TranslationOverlay()
        self._frame = RoiFrame()
        self._frame.geometryChanged.connect(self._on_frame_geometry)
        self._selector = RoiSelectionController(self)
        self._selector.selected.connect(self._on_selected)
        self._selector.cancelled.connect(self._on_selection_cancelled)

        self._join_timer = QTimer(self)
        self._join_timer.setInterval(100)
        self._join_timer.timeout.connect(self._poll_worker_exit)

        self._build_ui()
        self._worker.start()
        self._set_status("Hazır. Önce 'Alan seç' ile yakalama alanını belirleyin.")

    # --- Arayüz kurulumu ----------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("panel")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        title = QLabel("Ekran ROI çevirmeni")
        title.setObjectName("title")
        layout.addWidget(title)

        buttons = QHBoxLayout()
        self._select_button = QPushButton("Alan seç")
        self._start_button = QPushButton("Başlat")
        self._start_button.setObjectName("primary")
        self._start_button.setEnabled(False)
        buttons.addWidget(self._select_button)
        buttons.addWidget(self._start_button)
        layout.addLayout(buttons)

        actions = QHBoxLayout()
        self._remove_button = QPushButton("Kaldır")
        self._remove_button.setEnabled(False)
        self._remove_button.setToolTip("Seçili çeviri alanını ve çerçevesini kaldırır.")
        self._close_button = QPushButton("Kapat")
        actions.addWidget(self._remove_button)
        actions.addWidget(self._close_button)
        layout.addLayout(actions)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("Tarama aralığı"))
        self._interval_combo = QComboBox()
        for value in (0.5, 0.75, 1.0, 1.5, 2.0):
            self._interval_combo.addItem(f"{value:.2f} sn", value)
        self._interval_combo.setCurrentIndex(1)
        interval_row.addWidget(self._interval_combo)
        interval_row.addStretch(1)
        layout.addLayout(interval_row)

        opacity_row = QHBoxLayout()
        opacity_row.addWidget(QLabel("Çerçeve opaklığı"))
        self._opacity_slider = QSlider(Qt.Horizontal)
        self._opacity_slider.setRange(
            round(FRAME_OPACITY_MIN * 100), round(FRAME_OPACITY_MAX * 100)
        )
        self._opacity_slider.setSingleStep(1)
        self._opacity_slider.setPageStep(5)
        self._opacity_slider.setToolTip(
            "ROI çerçevesinin saydamlığı; ayar sonraki açılışta korunur."
        )
        self._opacity_value = QLabel("")
        self._opacity_value.setMinimumWidth(38)
        self._opacity_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        opacity_row.addWidget(self._opacity_slider, 1)
        opacity_row.addWidget(self._opacity_value)
        layout.addLayout(opacity_row)

        self._status_label = QLabel("")
        self._status_label.setObjectName("status")
        self._status_label.setWordWrap(True)
        self._error_label = QLabel("")
        self._error_label.setObjectName("error")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        layout.addWidget(self._error_label)

        self._fallback = QTextEdit()
        self._fallback.setObjectName("fallback")
        self._fallback.setReadOnly(True)
        self._fallback.setMinimumHeight(90)
        self._fallback.setPlaceholderText(
            "Çeviri penceresi ROI çevresine sığmadığında metin burada görünür."
        )
        layout.addWidget(self._fallback, 1)

        note = QLabel(
            "Gizlilik: ekran görüntüleri yerel kalır; yalnızca tanınan metin "
            "Google çeviri servisine gönderilir. İnternet bağlantısı gerekir."
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.setCentralWidget(central)

        self._select_button.clicked.connect(self._begin_selection)
        self._start_button.clicked.connect(self._toggle_running)
        self._remove_button.clicked.connect(self._remove_roi)
        self._close_button.clicked.connect(self._on_close)
        self._interval_combo.currentIndexChanged.connect(self._on_interval_changed)
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        self._opacity_slider.setValue(self._stored_opacity_percent())

        for key, slot in ((Qt.Key_Space, self._toggle_running), ("Ctrl+Q", self._on_close)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    # --- Seçim ve çalışma durumu -------------------------------------------
    def _begin_selection(self) -> None:
        if self._closing:
            return
        self._resume_after_selection = self._running
        self._worker.set_paused(True)
        self._set_status("Alan seçimi: sürükleyin, Esc ile iptal edin.")
        self._selector.start()

    def _on_selected(self, rect: QRect, screen: QScreen) -> None:
        self._screen = screen
        self._frame.set_screen(screen)
        self._frame.set_capture_geometry(rect)
        self._frame.show()
        exclude_from_capture(self._frame)
        self._capture_rect = self._frame.capture_geometry_global()
        self._worker.set_roi(self._frame.current_roi())
        self._overlay.set_translation("")
        self._overlay.hide()
        self._fallback.clear()
        self._fallback_active = False
        self._roi_active = True
        self._start_button.setEnabled(True)
        self._remove_button.setEnabled(True)
        self._apply_exclusion()
        self._reposition_overlay()
        self._set_running(self._resume_after_selection)
        self._resume_after_selection = False
        if not self._running:
            self._set_status("Alan hazır. 'Başlat' ile çeviriyi başlatın.")

    def _on_selection_cancelled(self) -> None:
        resume = self._resume_after_selection
        self._resume_after_selection = False
        if resume and self._worker.snapshot().roi is not None:
            self._set_running(True)
            if self._running:
                self._set_status("Alan seçimi iptal edildi; çalışmaya devam ediliyor.")
        else:
            self._set_running(False)
            self._set_status("Alan seçimi iptal edildi.")

    def _toggle_running(self) -> None:
        if self._closing:
            return
        if self._worker.snapshot().roi is None:
            self._set_status("Önce 'Alan seç' ile bir yakalama alanı belirleyin.")
            return
        self._set_running(not self._running)

    def _set_running(self, running: bool) -> None:
        if running and self._worker.snapshot().roi is None:
            return
        if running:
            self._apply_exclusion()
            if self._panel_conflicts() and not self._panel_excluded:
                self._running = False
                self._worker.set_paused(True)
                self._start_button.setText("Başlat")
                self._set_status(
                    "Kontrol paneli yakalama alanıyla çakışıyor: paneli taşıyın "
                    "veya ROI'yi küçültün."
                )
                return
        self._running = running
        self._worker.set_paused(not running)
        self._start_button.setText("Duraklat" if running else "Başlat")
        self._set_status(
            "Çalışıyor: ekran okunuyor ve çevriliyor."
            if running
            else "Duraklatıldı."
        )

    def _on_interval_changed(self) -> None:
        value = self._interval_combo.currentData()
        if value is None:
            return
        self._worker.set_poll_interval(float(value))
        if self._running:
            self._set_status(f"Çalışıyor: tarama aralığı {float(value):.2f} sn.")
        else:
            self._set_status(f"Tarama aralığı {float(value):.2f} sn.")

    def _stored_opacity_percent(self) -> int:
        """Kayıtlı opaklığı yüzde olarak okur; geçersiz değerde varsayılana döner."""
        default = round(FRAME_OPACITY_DEFAULT * 100)
        try:
            value = int(self._settings.value(OPACITY_KEY, default))
        except (TypeError, ValueError):
            return default
        low = round(FRAME_OPACITY_MIN * 100)
        high = round(FRAME_OPACITY_MAX * 100)
        return max(low, min(high, value))

    def _on_opacity_changed(self, percent: int) -> None:
        self._frame.set_opacity(percent / 100.0)
        self._opacity_value.setText(f"%{percent}")
        self._settings.setValue(OPACITY_KEY, int(percent))

    def _remove_roi(self) -> None:
        """Seçili alanı kaldırır ve çalışmayı durdurur."""
        if self._closing:
            return
        if self._worker.snapshot().roi is None:
            self._set_status("Kaldırılacak alan yok. Önce 'Alan seç' ile alan belirleyin.")
            return
        self._set_running(False)
        self._roi_active = False
        self._frame.hide()
        self._worker.set_roi(None)
        self._capture_rect = QRect()
        self._screen = None
        self._overlay.set_translation("")
        self._overlay.hide()
        self._fallback.clear()
        self._fallback_active = False
        self._start_button.setEnabled(False)
        self._remove_button.setEnabled(False)
        self._set_status("Alan kaldırıldı. Yeni alan için 'Alan seç'.")

    def _on_result(self, generation: int, text: str, truncated: bool) -> None:
        if self._closing:
            return
        if generation != self._worker.snapshot().generation:
            return
        self._error_label.setText("")
        if not text:
            self._overlay.set_translation("")
            self._overlay.hide()
            self._fallback.clear()
            self._fallback_active = False
            return
        if not self._overlay.place_near(self._capture_rect, self._screen):
            self._fallback.setPlainText(
                text + ("\n\n... (metin 4000 karakterde kısaltıldı)" if truncated else "")
            )
            self._fallback_active = True
            self._set_status(
                "Çeviri panelde gösteriliyor: ROI çevresinde yeterli boş alan yok, "
                "alanı küçültün."
            )
            return
        self._overlay.set_translation(text, truncated)
        self._fallback.clear()
        if self._fallback_active:
            self._fallback_active = False
            if self._running:
                self._set_status("Çalışıyor: ekran okunuyor ve çevriliyor.")

    def _on_frame_geometry(self, roi: Roi, capture: QRect, outer: QRect) -> None:
        if self._closing or not self._roi_active:
            return
        self._capture_rect = capture
        self._worker.set_roi(roi)
        self._reposition_overlay()
        self._enforce_conflict()

    def _on_error(self, message: str) -> None:
        self._error_label.setText(message)

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    # --- Pencere yerleşimi ve kapanış ---------------------------------------
    def _apply_exclusion(self) -> None:
        """Kendi pencerelerini yakalamadan hariç tutmayı dener (en iyi çaba)."""
        self._panel_excluded = exclude_from_capture(self)
        exclude_from_capture(self._overlay)
        exclude_from_capture(self._frame)

    def _panel_conflicts(self) -> bool:
        if self._capture_rect.isNull() or self.isMinimized():
            return False
        return self.frameGeometry().intersects(self._capture_rect)

    def _enforce_conflict(self) -> None:
        """Çalışırken panel ROI'ye girerse kendi çıktısını okumamak için duraklat."""
        if not self._running or self._closing or self._panel_excluded:
            return
        if not self._panel_conflicts():
            return
        self._set_running(False)
        self._set_status(
            "Kontrol paneli yakalama alanıyla çakışıyor: paneli taşıyın "
            "veya ROI'yi küçültün."
        )

    def _reposition_overlay(self) -> None:
        if self._capture_rect.isNull() or not self._overlay.isVisible():
            return
        if not self._overlay.place_near(self._capture_rect, self._screen):
            self._overlay.hide()

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._selector.close_all()
        self._overlay.hide()
        self._frame.hide()
        self._set_status("Kapatılıyor: worker bekleniyor...")
        self._worker.stop(timeout=0.0)
        self._join_attempts = 0
        self._join_timer.start()

    def _poll_worker_exit(self) -> None:
        """GUI'yi bloklamadan worker çıkışını yoklar.

        Worker hâlâ canlıysa (ör. OCR/ağ zaman aşımı) zorla çıkılmaz; sınırlı
        işlemler tamamlanana kadar yoklama sürer.
        """
        self._join_attempts += 1
        if self._worker.join(0.05):
            self._join_timer.stop()
            app = QGuiApplication.instance()
            if app is not None:
                app.quit()
            return
        if self._join_attempts % 50 == 0:
            self._set_status("Kapatılıyor: worker hâlâ bekleniyor...")

    def shutdown(self, timeout: float = 5.0) -> None:
        """Uygulama kapanırken worker'ı sınırlı süre bekler."""
        self._join_timer.stop()
        self._worker.stop(timeout=timeout)

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        super().moveEvent(event)
        self._enforce_conflict()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        super().resizeEvent(event)
        self._enforce_conflict()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        if not self._closing:
            self.hide()
            self._on_close()
            event.ignore()
            return
        super().closeEvent(event)
