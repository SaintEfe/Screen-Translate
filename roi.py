"""ROI çerçevesi ve ekran alanı seçimi.

Bu modüldeki tüm etkileşimler Qt olayları ile GUI thread'inde yürür.
Fiziksel ROI hesabı Windows'ta HWND tabanlı yapılır; global mantıksal
koordinatın DPR ile çarpılması karışık DPI'da yanlış sonuç verir.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Optional

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QRegion,
    QScreen,
)
from PySide6.QtWidgets import QWidget

from engine import Roi

# Çerçeve ölçüleri (mantıksal piksel). Yakalama alanı bu payların dışında kalır.
BORDER = 5          # sol/üst/alt/yan kenar kalınlığı
TOP_STRIP = 28      # sürükleme şeridi
GRIP = 16           # sağ alt boyutlandırma tutamağı ve sağ/alt pay
MIN_CAPTURE_W = 100
MIN_CAPTURE_H = 50
MIN_OUTER_W = MIN_CAPTURE_W + BORDER + GRIP
MIN_OUTER_H = MIN_CAPTURE_H + TOP_STRIP + GRIP

WDA_EXCLUDEFROMCAPTURE = 0x00000011
FRAME_COLOR = QColor(64, 196, 255, 235)
FRAME_TEXT = QColor(8, 22, 32)
FRAME_OPACITY_MIN = 0.15
FRAME_OPACITY_MAX = 1.0
FRAME_OPACITY_DEFAULT = 235 / 255.0

if sys.platform == "win32":  # pragma: no cover - platforma bağlı

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    # 64-bit HWND için c_void_p kullanılır; argtypes/restype açıkça tanımlanır.
    _user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    _user32.GetClientRect.restype = ctypes.c_bool
    _user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(_POINT)]
    _user32.ClientToScreen.restype = ctypes.c_bool
    _user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32.SetWindowDisplayAffinity.restype = ctypes.c_bool


def exclude_from_capture(widget: QWidget) -> bool:
    """Pencereyi ekran yakalamadan hariç tutmayı dener (Win10 2004+)."""
    if sys.platform != "win32":
        return False
    try:
        hwnd = ctypes.c_void_p(int(widget.winId()))
        return bool(_user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        return False


def physical_roi(widget: QWidget, capture: QRect, outer: QRect) -> Roi:
    """Mantıksal yakalama dikdörtgenini fiziksel ekran pikseline çevirir.

    Windows'ta istemci alanı (GetClientRect) ile ekran kökeni
    (ClientToScreen) alınır ve yerel paylar bu ölçeğe göre uygulanır.
    """
    if sys.platform == "win32":
        hwnd = ctypes.c_void_p(int(widget.winId()))
        rect = _RECT()
        point = _POINT(0, 0)
        ok = _user32.GetClientRect(hwnd, ctypes.byref(rect))
        ok = ok and _user32.ClientToScreen(hwnd, ctypes.byref(point))
        client_w = rect.right - rect.left
        client_h = rect.bottom - rect.top
        if ok and client_w > 0 and client_h > 0 and outer.width() > 0:
            scale_x = client_w / float(outer.width())
            scale_y = client_h / float(outer.height())
            # capture yerel (widget içi) koordinattır; ölçek yalnızca paylara uygulanır.
            return Roi(
                point.x + round(capture.left() * scale_x),
                point.y + round(capture.top() * scale_y),
                max(1, round(capture.width() * scale_x)),
                max(1, round(capture.height() * scale_y)),
            )
    # Windows dışı en iyi çaba: ekran DPR'si ile ölçekleme.
    # Yerel Wayland'de ekran yakalama kısıtlıdır; X11 varsayılır.
    screen = widget.screen() or QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen else 1.0
    return Roi(
        round((outer.left() + capture.left()) * dpr),
        round((outer.top() + capture.top()) * dpr),
        max(1, round(capture.width() * dpr)),
        max(1, round(capture.height() * dpr)),
    )


class RoiFrame(QWidget):
    """İçi boş, sürüklenebilir ve boyutlandırılabilir ROI çerçevesi.

    Merkez tamamen saydamdır ve maske ile giriş olaylarından çıkarılır;
    böylece yakalama alanı karartılmaz ve tıklamalar alt pencereye geçer.
    """

    geometryChanged = Signal(object, object, object)  # Roi, QRect, QRect

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setMinimumSize(MIN_OUTER_W, MIN_OUTER_H)
        self._screen: Optional[QScreen] = None
        self._press_global: Optional[QPoint] = None
        self._press_rect: Optional[QRect] = None
        self._resizing = False
        self._label = "Çeviri alanı"
        self._opacity = FRAME_OPACITY_DEFAULT

    # --- Geometri -----------------------------------------------------------
    def capture_rect(self) -> QRect:
        """Çerçeve, başlık ve tutamak hariç yakalama dikdörtgeni."""
        return self.rect().adjusted(BORDER, TOP_STRIP, -GRIP, -GRIP)

    @staticmethod
    def outer_for_capture(rect: QRect) -> QRect:
        """Yakalama dikdörtgenini çerçeve dış geometrisine genişletir."""
        return rect.adjusted(-BORDER, -TOP_STRIP, GRIP, GRIP)

    def set_screen(self, screen: Optional[QScreen]) -> None:
        self._screen = screen

    def opacity(self) -> float:
        """Çerçeve dolgusunun opaklığı (0.15 - 1.0)."""
        return self._opacity

    def set_opacity(self, value: float) -> None:
        """Çerçeve opaklığını sınırlar içinde uygular."""
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        self._opacity = max(FRAME_OPACITY_MIN, min(FRAME_OPACITY_MAX, numeric))
        self.update()

    def _frame_color(self) -> QColor:
        color = QColor(FRAME_COLOR)
        color.setAlphaF(self._opacity)
        return color

    def active_screen(self) -> Optional[QScreen]:
        return self._screen or self.screen() or QGuiApplication.primaryScreen()

    def _clamp_outer(self, rect: QRect) -> QRect:
        """Dış geometriyi seçili ekranın kullanılabilir alanına sığdırır."""
        screen = self.active_screen()
        if screen is None:
            return rect
        avail = screen.availableGeometry()
        width = max(MIN_OUTER_W, min(rect.width(), avail.width()))
        height = max(MIN_OUTER_H, min(rect.height(), avail.height()))
        x = max(avail.left(), min(rect.left(), avail.right() - width + 1))
        y = max(avail.top(), min(rect.top(), avail.bottom() - height + 1))
        return QRect(x, y, width, height)

    def set_capture_geometry(self, rect: QRect) -> None:
        """Seçilen dikdörtgeni yakalama alanı olarak uygular."""
        self.setGeometry(self._clamp_outer(self.outer_for_capture(rect)))

    def current_roi(self) -> Roi:
        return physical_roi(self, self.capture_rect(), self.geometry())

    def capture_geometry_global(self) -> QRect:
        """Yakalama alanının ekran (global) koordinatlarındaki karşılığı."""
        return self.geometry().adjusted(BORDER, TOP_STRIP, -GRIP, -GRIP)

    def _emit_geometry(self) -> None:
        self.geometryChanged.emit(
            self.current_roi(), self.capture_geometry_global(), self.geometry()
        )

    def _update_mask(self) -> None:
        """Merkezi giriş olaylarından çıkarır (içi boş çerçeve)."""
        self.setMask(QRegion(self.rect()).subtracted(QRegion(self.capture_rect())))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        super().resizeEvent(event)
        self._update_mask()
        self._emit_geometry()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        super().moveEvent(event)
        self._emit_geometry()

    # --- Çizim --------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        painter = QPainter(self)
        # Kenar dolgusu için yumuşatma kapalı: yakalama alanına piksel taşmasın.
        painter.setRenderHint(QPainter.Antialiasing, False)
        # Dış dikdörtgenden yakalama alanını oyan halka: kenar + şerit + tutamak.
        path = QPainterPath()
        path.addRect(QRectF(self.rect()))
        path.addRect(QRectF(self.capture_rect()))
        path.setFillRule(Qt.OddEvenFill)
        painter.fillPath(path, self._frame_color())

        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(FRAME_TEXT)
        painter.drawText(
            QRect(BORDER + 8, 0, max(0, self.width() - BORDER - GRIP - 16), TOP_STRIP),
            Qt.AlignVCenter | Qt.AlignLeft,
            self._label,
        )

        grip = self._grip_rect()
        painter.setPen(QPen(FRAME_TEXT, 2))
        for offset in (4, 8, 12):
            painter.drawLine(
                grip.right() - offset,
                grip.bottom() - 2,
                grip.right() - 2,
                grip.bottom() - offset,
            )

    # --- Fare etkileşimi (yalnızca GUI thread) ------------------------------
    def _grip_rect(self) -> QRect:
        return QRect(self.width() - GRIP, self.height() - GRIP, GRIP, GRIP)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        if event.button() != Qt.LeftButton:
            return
        self._press_global = event.globalPosition().toPoint()
        self._press_rect = self.geometry()
        self._resizing = self._grip_rect().contains(event.position().toPoint())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        position = event.position().toPoint()
        if self._press_global is None or self._press_rect is None:
            self.setCursor(
                Qt.SizeFDiagCursor
                if self._grip_rect().contains(position)
                else Qt.SizeAllCursor
            )
            return
        delta = event.globalPosition().toPoint() - self._press_global
        if self._resizing:
            size = self._press_rect.size() + QSize(delta.x(), delta.y())
            self.setGeometry(self._clamp_outer(QRect(self._press_rect.topLeft(), size)))
        else:
            self.setGeometry(self._clamp_outer(self._press_rect.translated(delta)))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        self._press_global = None
        self._press_rect = None
        self._resizing = False
        self._emit_geometry()
        event.accept()


class RoiSelector(QWidget):
    """Tek bir ekranı kaplayan, karartılmış lastik bant seçici."""

    selected = Signal(object, object)  # QRect (global mantıksal), QScreen
    cancelled = Signal()

    def __init__(self, screen: QScreen) -> None:
        super().__init__(None)
        self._screen = screen
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(screen.geometry())
        self._origin: Optional[QPoint] = None
        self._current: Optional[QPoint] = None

    def screen_handle(self) -> QScreen:
        return self._screen

    def show_for_selection(self) -> None:
        self.setGeometry(self._screen.geometry())
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.OtherFocusReason)

    def _band(self) -> QRect:
        if self._origin is None or self._current is None:
            return QRect()
        left = min(self._origin.x(), self._current.x())
        top = min(self._origin.y(), self._current.y())
        width = abs(self._current.x() - self._origin.x())
        height = abs(self._current.y() - self._origin.y())
        return QRect(left, top, width, height)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))
        band = self._band()
        if band.isValid() and not band.isEmpty():
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(band, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor(64, 196, 255), 2))
            painter.drawRect(band.adjusted(1, 1, -1, -1))
            painter.setPen(QColor(235, 245, 255))
            painter.drawText(
                band.adjusted(8, 8, -8, -8),
                Qt.AlignLeft | Qt.AlignTop,
                f"{band.width()} × {band.height()}",
            )
        painter.setPen(QColor(235, 245, 255))
        painter.drawText(
            self.rect().adjusted(0, 24, 0, 0),
            Qt.AlignHCenter | Qt.AlignTop,
            "Alan seçmek için sürükleyin · Esc ile iptal",
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        if event.button() != Qt.LeftButton:
            return
        self._origin = event.position().toPoint()
        self._current = self._origin
        self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        if self._origin is None:
            return
        self._current = event.position().toPoint()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        if event.button() != Qt.LeftButton or self._origin is None:
            return
        band = self._band()
        self._origin = None
        self._current = None
        self.update()
        if band.width() >= MIN_CAPTURE_W and band.height() >= MIN_CAPTURE_H:
            global_rect = QRect(self.mapToGlobal(band.topLeft()), band.size())
            self.selected.emit(global_rect, self._screen)
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt adlandırması
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class RoiSelectionController(QObject):
    """Her ekran için bir seçici açar ve yaşam döngüsünü yönetir."""

    selected = Signal(object, object)  # QRect (global mantıksal), QScreen
    cancelled = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._selectors: list[RoiSelector] = []

    def is_active(self) -> bool:
        return bool(self._selectors)

    def start(self) -> None:
        self.close_all()
        for screen in QGuiApplication.screens():
            selector = RoiSelector(screen)
            selector.selected.connect(self._on_selected)
            selector.cancelled.connect(self._on_cancelled)
            selector.show_for_selection()
            self._selectors.append(selector)

    def close_all(self) -> None:
        selectors, self._selectors = self._selectors, []
        for selector in selectors:
            selector.hide()
            selector.deleteLater()

    def _on_selected(self, rect: QRect, screen: QScreen) -> None:
        self.close_all()
        self.selected.emit(rect, screen)

    def _on_cancelled(self) -> None:
        self.close_all()
        self.cancelled.emit()
