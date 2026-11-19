"""Uygulama giriş noktası.

DPI farkındalığı QApplication oluşturulmadan ÖNCE ayarlanmalıdır; aksi
halde Windows mantıksal/fiziksel piksel dönüşümü tutarsız olur.
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Optional, Sequence

APP_NAME = "Screen Translate"
APP_AUTHOR = "SaintEfe"


def resource_path(name: str) -> str:
    """Kaynak ağacında ve PyInstaller paketinde aynı dosyayı bulur."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def enable_dpi_awareness() -> None:
    """Windows'ta monitör başına v2 DPI farkındalığını etkinleştirir."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_int
        if shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except Exception:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Uygulamayı başlatır ve olay döngüsünü çalıştırır."""
    enable_dpi_awareness()

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from ui import ControlPanel

    app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_AUTHOR)
    icon_path = resource_path("icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    # Seçici pencereleri kapanınca uygulama erken sonlanmasın.
    app.setQuitOnLastWindowClosed(False)

    panel = ControlPanel()
    panel.show()
    app.aboutToQuit.connect(panel.shutdown)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
