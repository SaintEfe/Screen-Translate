"""Arayüz testleri (offscreen): çerçeve, yerleşim, panel yaşam döngüsü.

Gerçek ekran içeriği okunmaz; tüm etkileşimler programatik Qt olaylarıdır.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import engine  # noqa: E402
import roi as roi_module  # noqa: E402
import ui as ui_module  # noqa: E402
from roi import RoiFrame, RoiSelector  # noqa: E402

APP = QApplication.instance() or QApplication([])


def mouse_event(kind, local: QPoint, global_pos: QPoint, button=Qt.LeftButton):
    """Belirtilen global konumla fare olayı üretir."""
    return QMouseEvent(
        kind,
        QPointF(local),
        QPointF(global_pos),
        button,
        button,
        Qt.NoModifier,
    )


def press(widget, local: QPoint, global_pos: QPoint) -> None:
    widget.mousePressEvent(mouse_event(QEvent.MouseButtonPress, local, global_pos))


def move(widget, local: QPoint, global_pos: QPoint) -> None:
    widget.mouseMoveEvent(mouse_event(QEvent.MouseMove, local, global_pos, Qt.NoButton))


def release(widget, local: QPoint, global_pos: QPoint) -> None:
    widget.mouseReleaseEvent(
        mouse_event(QEvent.MouseButtonRelease, local, global_pos)
    )


class FrameGeometryTests(unittest.TestCase):
    def setUp(self):
        self.frame = RoiFrame()
        self.frame.setGeometry(100, 100, 300, 200)
        self.addCleanup(self.frame.deleteLater)

    def test_capture_inset_excludes_frame_title_and_grip(self):
        capture = self.frame.capture_rect()
        self.assertEqual(capture.left(), roi_module.BORDER)
        self.assertEqual(capture.top(), roi_module.TOP_STRIP)
        self.assertEqual(capture.width(), 300 - roi_module.BORDER - roi_module.GRIP)
        self.assertEqual(capture.height(), 200 - roi_module.TOP_STRIP - roi_module.GRIP)
        self.assertFalse(capture.contains(self.frame._grip_rect().center()))

    def test_outer_for_capture_roundtrip(self):
        capture = QRect(200, 200, 240, 120)
        outer = RoiFrame.outer_for_capture(capture)
        self.frame.setGeometry(outer)
        self.assertEqual(self.frame.capture_rect().size(), capture.size())
        self.assertEqual(
            self.frame.capture_rect().topLeft(),
            QPoint(roi_module.BORDER, roi_module.TOP_STRIP),
        )

    def test_drag_moves_frame_by_delta(self):
        press(self.frame, QPoint(10, 10), QPoint(110, 110))
        move(self.frame, QPoint(60, 40), QPoint(160, 140))
        release(self.frame, QPoint(60, 40), QPoint(160, 140))
        self.assertEqual(self.frame.geometry(), QRect(150, 130, 300, 200))

    def test_resize_from_grip(self):
        grip = self.frame._grip_rect().center()
        global_grip = self.frame.mapToGlobal(grip)
        press(self.frame, grip, global_grip)
        move(self.frame, grip + QPoint(40, 20), global_grip + QPoint(40, 20))
        release(self.frame, grip + QPoint(40, 20), global_grip + QPoint(40, 20))
        self.assertEqual(self.frame.size().width(), 340)
        self.assertEqual(self.frame.size().height(), 220)

    def test_resize_respects_minimum(self):
        grip = self.frame._grip_rect().center()
        global_grip = self.frame.mapToGlobal(grip)
        press(self.frame, grip, global_grip)
        move(self.frame, grip - QPoint(500, 500), global_grip - QPoint(500, 500))
        release(self.frame, grip - QPoint(500, 500), global_grip - QPoint(500, 500))
        self.assertGreaterEqual(self.frame.width(), roi_module.MIN_OUTER_W)
        self.assertGreaterEqual(self.frame.height(), roi_module.MIN_OUTER_H)


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.overlay = ui_module.TranslationOverlay()
        self.addCleanup(self.overlay.deleteLater)
        self.screen = APP.primaryScreen()
        self.avail = self.screen.availableGeometry()

    def test_place_near_roi_without_intersection(self):
        capture = QRect(self.avail.left() + 50, self.avail.top() + 50, 200, 100)
        self.assertTrue(self.overlay.place_near(capture, self.screen))
        self.assertFalse(self.overlay.geometry().intersects(capture))
        self.assertTrue(self.avail.contains(self.overlay.geometry()))

    def test_fallback_when_no_space(self):
        capture = QRect(self.avail)
        self.assertFalse(self.overlay.place_near(capture, self.screen))
        self.assertFalse(self.overlay.isVisible())

    def test_plain_text_only(self):
        raw = "<b>kalın</b> & <script>x</script>"
        self.overlay.set_translation(raw)
        self.assertEqual(self.overlay._view.toPlainText(), raw)

    def test_truncation_notice(self):
        self.overlay.set_translation("metin", truncated=True)
        self.assertIn("kısaltıldı", self.overlay._view.toPlainText())


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.screen = APP.primaryScreen()
        self.selector = RoiSelector(self.screen)
        self.addCleanup(self.selector.deleteLater)
        self.selected = []
        self.cancelled = []
        self.selector.selected.connect(lambda rect, scr: self.selected.append(rect))
        self.selector.cancelled.connect(lambda: self.cancelled.append(True))
        self.selector.show_for_selection()

    def test_drag_emits_global_rect(self):
        press(self.selector, QPoint(40, 60), QPoint(40, 60))
        move(self.selector, QPoint(340, 260), QPoint(340, 260))
        release(self.selector, QPoint(340, 260), QPoint(340, 260))
        self.assertEqual(len(self.selected), 1)
        self.assertEqual(self.selected[0].size(), QRect(0, 0, 300, 200).size())

    def test_too_small_drag_ignored(self):
        press(self.selector, QPoint(40, 60), QPoint(40, 60))
        move(self.selector, QPoint(50, 70), QPoint(50, 70))
        release(self.selector, QPoint(50, 70), QPoint(50, 70))
        self.assertEqual(self.selected, [])

    def test_escape_cancels(self):
        QTest.keyClick(self.selector, Qt.Key_Escape)
        self.assertEqual(self.cancelled, [True])


class PhysicalRoiTests(unittest.TestCase):
    def test_fallback_scales_by_screen_dpr(self):
        class FakeScreen:
            def devicePixelRatio(self):
                return 2.0

        class FakeWidget:
            def winId(self):
                return 0

            def screen(self):
                return FakeScreen()

        result = roi_module.physical_roi(
            FakeWidget(), QRect(10, 20, 100, 50), QRect(0, 0, 200, 100)
        )
        self.assertEqual(
            (result.left, result.top, result.width, result.height),
            (20, 40, 200, 100),
        )


class FakeWorker:
    """Arayüz testleri için thread'siz worker yerine geçen sınıf."""

    def __init__(self, *args, **kwargs):
        self.paused = True
        self.roi = None
        self.generation = 0
        self.interval = None
        self.stopped = False
        self.started = False

    def start(self):
        self.started = True

    def stop(self, timeout=12.0):
        self.stopped = True
        return True

    def join(self, timeout=12.0):
        return True

    def is_running(self):
        return True

    def snapshot(self):
        return engine.WorkerState(self.roi, self.paused, self.generation)

    def set_roi(self, roi):
        self.roi = roi
        self.generation += 1

    def set_paused(self, paused):
        self.paused = paused
        self.generation += 1

    def set_poll_interval(self, seconds):
        self.interval = seconds


class PanelTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ui_module, "TranslationWorker", FakeWorker)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.panel = ui_module.ControlPanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.show()
        self.screen = APP.primaryScreen()

    def test_start_disabled_before_roi(self):
        self.assertFalse(self.panel._start_button.isEnabled())
        self.panel._toggle_running()
        self.assertIn("Alan seç", self.panel._status_label.text())

    def test_selection_enables_start_and_sets_roi(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.assertTrue(self.panel._start_button.isEnabled())
        self.assertIsNotNone(self.panel._worker.roi)
        self.assertGreater(self.panel._worker.roi.width, 0)
        self.assertTrue(self.panel._frame.isVisible())
        self.assertEqual(
            self.panel._frame.capture_rect().size(),
            QRect(0, 0, 240, 120).size(),
        )

    def test_pause_resume_toggle(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._toggle_running()
        self.assertFalse(self.panel._worker.paused)
        self.assertEqual(self.panel._start_button.text(), "Duraklat")
        self.panel._toggle_running()
        self.assertTrue(self.panel._worker.paused)
        self.assertEqual(self.panel._start_button.text(), "Başlat")

    def test_selection_cancelled_keeps_status(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._toggle_running()
        self.panel._begin_selection()
        self.panel._selector._on_cancelled()
        self.assertIn("iptal", self.panel._status_label.text())
        self.assertFalse(self.panel._worker.paused)

    def test_interval_change_keeps_running_status(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._toggle_running()
        self.panel._interval_combo.setCurrentIndex(2)
        self.assertIn("Çalışıyor", self.panel._status_label.text())

    def test_fallback_status_clears_when_overlay_returns(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._toggle_running()
        generation = self.panel._worker.generation
        self.panel._capture_rect = QRect(self.screen.availableGeometry())
        self.panel._on_result(generation, "Yedek metin", False)
        self.assertIn("panelde", self.panel._status_label.text())

        self.panel._capture_rect = QRect(420, 300, 240, 120)
        self.panel._on_result(generation, "Yedek metin", False)
        self.assertIn("Çalışıyor", self.panel._status_label.text())
        self.assertEqual(self.panel._fallback.toPlainText(), "")

    def test_panel_conflict_pauses_with_status(self):
        self.panel._panel_excluded = False
        self.panel._on_selected(QRect(0, 0, 240, 120), self.screen)
        self.panel._toggle_running()
        self.assertTrue(self.panel._worker.paused)
        self.assertIn("çakışıyor", self.panel._status_label.text())

    def test_stale_result_ignored(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        stale = self.panel._worker.generation - 1
        self.panel._on_result(stale, "ESKİ ÇEVİRİ", False)
        self.assertEqual(self.panel._overlay._view.toPlainText(), "")
        self.assertEqual(self.panel._fallback.toPlainText(), "")

    def test_result_rendered_in_overlay(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._on_result(self.panel._worker.generation, "Merhaba dünya", False)
        self.assertEqual(self.panel._overlay._view.toPlainText(), "Merhaba dünya")
        self.assertTrue(self.panel._overlay.isVisible())

    def test_result_falls_back_to_panel_when_no_space(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._capture_rect = QRect(self.screen.availableGeometry())
        self.panel._on_result(self.panel._worker.generation, "Yedek metin", False)
        self.assertFalse(self.panel._overlay.isVisible())
        self.assertEqual(self.panel._fallback.toPlainText(), "Yedek metin")

    def test_close_stops_worker_without_blocking(self):
        self.panel._on_close()
        self.assertTrue(self.panel._worker.stopped)
        self.assertTrue(self.panel._closing)
        self.assertTrue(self.panel._join_timer.isActive())
        self.panel._join_timer.stop()


class PanelRegressionTests(unittest.TestCase):
    """Nesil, çakışma ve kapanış düzeltmelerinin arayüz testleri."""

    def setUp(self):
        patcher = mock.patch.object(ui_module, "TranslationWorker", FakeWorker)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.panel = ui_module.ControlPanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.show()
        self.screen = APP.primaryScreen()

    def test_reselect_same_text_redisplays_overlay(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        gen = self.panel._worker.generation
        self.panel._on_result(gen, "Merhaba dünya", False)
        self.assertTrue(self.panel._overlay.isVisible())

        # Aynı alan yeniden seçilir: eski görünüm temizlenir.
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.assertFalse(self.panel._overlay.isVisible())
        self.assertEqual(self.panel._overlay._view.toPlainText(), "")
        self.assertEqual(self.panel._fallback.toPlainText(), "")

        # Önbellekten gelen aynı metin yeni nesilde yeniden görünür.
        new_gen = self.panel._worker.generation
        self.assertNotEqual(new_gen, gen)
        self.panel._on_result(new_gen, "Merhaba dünya", False)
        self.assertTrue(self.panel._overlay.isVisible())
        self.assertEqual(self.panel._overlay._view.toPlainText(), "Merhaba dünya")

    def test_selection_to_blank_clears_old_text(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._on_result(self.panel._worker.generation, "Eski çeviri", False)
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._on_result(self.panel._worker.generation, "", False)
        self.assertEqual(self.panel._overlay._view.toPlainText(), "")
        self.assertFalse(self.panel._overlay.isVisible())
        self.assertEqual(self.panel._fallback.toPlainText(), "")

    def test_frame_move_over_panel_pauses(self):
        self.panel._panel_excluded = False
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._toggle_running()
        self.assertFalse(self.panel._worker.paused)

        overlap = self.panel.frameGeometry()
        self.panel._on_frame_geometry(
            engine.Roi(overlap.left(), overlap.top(), overlap.width(), overlap.height()),
            overlap,
            overlap,
        )
        self.assertTrue(self.panel._worker.paused)
        self.assertFalse(self.panel._running)
        self.assertIn("çakışıyor", self.panel._status_label.text())

    def test_panel_resize_into_roi_pauses(self):
        self.panel._panel_excluded = False
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._toggle_running()
        self.assertFalse(self.panel._worker.paused)

        self.panel._capture_rect = QRect(0, 0, 4000, 4000)
        self.panel.resize(500, 500)
        self.assertTrue(self.panel._worker.paused)
        self.assertIn("çakışıyor", self.panel._status_label.text())

    def test_minimize_does_not_pause(self):
        self.panel._panel_excluded = False
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._toggle_running()
        self.panel._capture_rect = QRect(0, 0, 4000, 4000)
        self.panel.showMinimized()
        self.assertFalse(self.panel._worker.paused)

    def test_result_ignored_while_closing(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._on_close()
        self.panel._join_timer.stop()
        self.panel._on_result(self.panel._worker.generation, "Geç gelen", False)
        self.assertFalse(self.panel._overlay.isVisible())
        self.assertEqual(self.panel._fallback.toPlainText(), "")

    def test_success_clears_error_label(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._on_error("Çeviri isteği başarısız oldu.")
        self.assertNotEqual(self.panel._error_label.text(), "")
        self.panel._on_result(self.panel._worker.generation, "Merhaba", False)
        self.assertEqual(self.panel._error_label.text(), "")

    def test_poll_worker_exit_waits_while_worker_alive(self):
        self.panel._on_close()
        self.panel._worker.join = lambda timeout=0.0: False
        for _ in range(200):
            self.panel._poll_worker_exit()
        self.assertTrue(self.panel._join_timer.isActive())
        self.assertIn("Kapatılıyor", self.panel._status_label.text())
        self.panel._join_timer.stop()


class FrameOpacityTests(unittest.TestCase):
    def setUp(self):
        self.frame = RoiFrame()
        self.addCleanup(self.frame.deleteLater)

    def test_default_opacity(self):
        self.assertAlmostEqual(self.frame.opacity(), roi_module.FRAME_OPACITY_DEFAULT)

    def test_opacity_clamped_to_bounds(self):
        self.frame.set_opacity(0.0)
        self.assertAlmostEqual(self.frame.opacity(), roi_module.FRAME_OPACITY_MIN)
        self.frame.set_opacity(9.0)
        self.assertAlmostEqual(self.frame.opacity(), roi_module.FRAME_OPACITY_MAX)

    def test_invalid_opacity_ignored(self):
        self.frame.set_opacity(0.5)
        self.frame.set_opacity("geçersiz")
        self.assertAlmostEqual(self.frame.opacity(), 0.5)

    def test_frame_color_uses_opacity(self):
        self.frame.set_opacity(0.4)
        self.assertAlmostEqual(self.frame._frame_color().alphaF(), 0.4, places=2)


class RemoveRoiTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ui_module, "TranslationWorker", FakeWorker)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.panel = ui_module.ControlPanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.show()
        self.screen = APP.primaryScreen()

    def test_remove_clears_roi_and_disables_buttons(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._toggle_running()
        self.panel._on_result(self.panel._worker.generation, "Merhaba", False)

        self.panel._remove_roi()
        self.assertIsNone(self.panel._worker.roi)
        self.assertFalse(self.panel._start_button.isEnabled())
        self.assertFalse(self.panel._remove_button.isEnabled())
        self.assertFalse(self.panel._frame.isVisible())
        self.assertFalse(self.panel._overlay.isVisible())
        self.assertEqual(self.panel._fallback.toPlainText(), "")
        self.assertIn("kaldırıldı", self.panel._status_label.text())
        self.assertTrue(self.panel._worker.paused)

    def test_remove_without_roi_reports_status(self):
        self.panel._remove_roi()
        self.assertIn("Kaldırılacak alan yok", self.panel._status_label.text())

    def test_geometry_ignored_after_remove(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._remove_roi()
        rect = QRect(10, 10, 200, 100)
        self.panel._on_frame_geometry(
            engine.Roi(10, 10, 200, 100), rect, rect
        )
        self.assertIsNone(self.panel._worker.roi)

    def test_reselect_after_remove_reenables(self):
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.panel._remove_roi()
        self.panel._on_selected(QRect(420, 300, 240, 120), self.screen)
        self.assertIsNotNone(self.panel._worker.roi)
        self.assertTrue(self.panel._start_button.isEnabled())
        self.assertTrue(self.panel._remove_button.isEnabled())


class OpacityPanelTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ui_module, "TranslationWorker", FakeWorker)
        patcher.start()
        self.addCleanup(patcher.stop)

        handle = tempfile.NamedTemporaryFile(suffix=".ini", delete=False)
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        path = handle.name
        real = QSettings

        def factory(*_args, **_kwargs):
            return real(path, real.IniFormat)

        settings_patcher = mock.patch.object(ui_module, "QSettings", factory)
        settings_patcher.start()
        self.addCleanup(settings_patcher.stop)

        self.panel = ui_module.ControlPanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.show()

    def test_slider_updates_frame_and_label(self):
        self.panel._opacity_slider.setValue(40)
        self.assertAlmostEqual(self.panel._frame.opacity(), 0.40, places=2)
        self.assertEqual(self.panel._opacity_value.text(), "%40")

    def test_opacity_persisted_across_panels(self):
        self.panel._opacity_slider.setValue(55)
        second = ui_module.ControlPanel()
        self.addCleanup(second.deleteLater)
        self.assertEqual(second._opacity_slider.value(), 55)
        self.assertAlmostEqual(second._frame.opacity(), 0.55, places=2)

    def test_invalid_stored_value_falls_back(self):
        self.panel._settings.setValue(ui_module.OPACITY_KEY, "bozuk")
        self.assertEqual(
            self.panel._stored_opacity_percent(),
            round(roi_module.FRAME_OPACITY_DEFAULT * 100),
        )


if __name__ == "__main__":
    unittest.main()
