"""Motor testleri: sahte OCR/çeviri ile gerçek worker hattı.

Hiçbir test gerçek ağa veya gerçek Tesseract kurulumuna bağlı değildir.
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import engine
from engine import Roi, TranslationWorker, normalize_text, preprocess_image


class FakeTranslator:
    """googletrans yerine geçen asenkron sahte istemci."""

    def __init__(self, mapping=None, fail_times=0, delay=0.0):
        self.mapping = mapping or {}
        self.fail_times = fail_times
        self.delay = delay
        self.calls = []
        self.times = []
        self.closed = False

    async def translate(self, text, src="en", dest="tr"):
        self.calls.append(text)
        self.times.append(time.monotonic())
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("sahte ağ hatası")
        return SimpleNamespace(text=self.mapping.get(text, f"TR:{text}"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True
        return False


class FakeOcr:
    """pytesseract yerine geçen sayaçlı sahte OCR."""

    def __init__(self, text=""):
        self.text = text
        self.calls = 0

    def __call__(self, image, command, lang):
        self.calls += 1
        return self.text


class FakeGrabber:
    """mss yerine geçen, sabit beyaz görüntü üreten sahte yakalayıcı."""

    def __init__(self):
        self.calls = 0

    def __call__(self, roi):
        self.calls += 1
        return Image.new("RGB", (max(1, roi.width), max(1, roi.height)), "white")


def wait_until(predicate, timeout=8.0, interval=0.02):
    """Koşul sağlanana kadar sınırlı süre bekler."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def make_worker(translator, ocr_text="Hello", poll=0.5):
    """Başlatılmamış worker ve toplanan sinyal listeleri üretir."""
    results, statuses, errors = [], [], []
    grabber, ocr = FakeGrabber(), FakeOcr(ocr_text)
    worker = TranslationWorker(
        lambda gen, text, trunc: results.append((gen, text, trunc)),
        statuses.append,
        errors.append,
        poll_interval=poll,
        grabber=grabber,
        ocr_runner=ocr,
        translator_factory=lambda: translator,
    )
    return worker, results, statuses, errors, grabber, ocr


class WorkerHarness:
    """Worker'ı sahte bağımlılıklarla başlatan test yardımcısı."""

    def __init__(self, translator, ocr_text="Hello", poll=0.5):
        (
            self.worker,
            self.results,
            self.statuses,
            self.errors,
            self.grabber,
            self.ocr,
        ) = make_worker(translator, ocr_text, poll)
        # Tesseract kurulumu bu testlerde doğrulanmaz (gerçek kurulum yok).
        self._patch = mock.patch("engine.validate_ocr_setup", lambda *a, **k: None)
        self._patch.start()
        self.worker.start()

    def start_roi(self, roi=None):
        self.worker.set_roi(roi or Roi(0, 0, 200, 100))
        self.worker.set_paused(False)

    def close(self):
        self.worker.stop(3.0)
        self._patch.stop()


class NormalizeTests(unittest.TestCase):
    def test_normalize_keeps_line_breaks(self):
        raw = "  Hello   world \r\n\r\n\r\n  ikinci\t satır  \n"
        self.assertEqual(normalize_text(raw), "Hello world\n\nikinci satır")

    def test_normalize_blank(self):
        self.assertEqual(normalize_text("   \n\n \t "), "")


class PreprocessTests(unittest.TestCase):
    def test_grayscale_and_contrast(self):
        image = Image.new("L", (64, 8))
        image.putdata([100 + (i % 56) for i in range(64 * 8)])
        out = preprocess_image(image)
        self.assertEqual(out.mode, "L")
        self.assertEqual(out.getextrema(), (0, 255))

    def test_size_cap(self):
        image = Image.new("RGB", (3000, 100), (10, 200, 30))
        out = preprocess_image(image)
        self.assertEqual(out.size[0], engine.MAX_OCR_SIDE)
        self.assertLessEqual(max(out.size), engine.MAX_OCR_SIDE)


class CacheTests(unittest.TestCase):
    def test_lru_bound(self):
        worker, *_ = make_worker(FakeTranslator())
        for index in range(engine.CACHE_LIMIT + 5):
            worker._store(f"metin-{index}", f"TR-{index}", 0)
        self.assertEqual(len(worker._cache), engine.CACHE_LIMIT)
        self.assertNotIn("metin-0", worker._cache)
        self.assertIn(f"metin-{engine.CACHE_LIMIT + 4}", worker._cache)

    def test_store_records_given_generation(self):
        worker, *_ = make_worker(FakeTranslator())
        worker._store("metin", "TR", 7)
        self.assertEqual(worker._last_text_generation, 7)
        self.assertEqual(worker._last_text, "metin")
        self.assertEqual(worker._last_translation, "TR")


class PipelineTests(unittest.TestCase):
    def test_dedupe_skips_unchanged_text(self):
        translator = FakeTranslator()
        harness = WorkerHarness(translator, "Hello")
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: len(harness.results) >= 1))
        time.sleep(1.2)
        self.assertEqual(translator.calls, ["Hello"])
        self.assertEqual(harness.results[0][1], "TR:Hello")

    def test_blank_clears_then_same_text_redisplays(self):
        translator = FakeTranslator()
        harness = WorkerHarness(translator, "Hello")
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: len(harness.results) >= 1))

        harness.ocr.text = ""
        self.assertTrue(wait_until(lambda: any(r[1] == "" for r in harness.results)))

        harness.ocr.text = "Hello"
        self.assertTrue(
            wait_until(
                lambda: len([r for r in harness.results if r[1] == "TR:Hello"]) >= 2
            )
        )
        # Önbellekten geldiği için ikinci ağ isteği yok.
        self.assertEqual(translator.calls, ["Hello"])

    def test_failure_not_cached_and_retried(self):
        translator = FakeTranslator(fail_times=1)
        harness = WorkerHarness(translator, "Hello")
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: len(harness.results) >= 1, timeout=10))
        self.assertEqual(translator.calls, ["Hello", "Hello"])
        self.assertTrue(harness.errors)
        self.assertEqual(harness.results[0][1], "TR:Hello")

    def test_stale_generation_discarded(self):
        translator = FakeTranslator(delay=0.8)
        harness = WorkerHarness(translator, "Eski metin")
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: len(translator.calls) >= 1))

        harness.ocr.text = "Yeni metin"
        harness.worker.set_roi(Roi(10, 10, 200, 100))
        self.assertTrue(
            wait_until(lambda: any(r[1] == "TR:Yeni metin" for r in harness.results))
        )
        self.assertFalse(any(r[1] == "TR:Eski metin" for r in harness.results))

    def test_pause_stops_ocr(self):
        harness = WorkerHarness(FakeTranslator(), "Hello")
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: harness.grabber.calls >= 1))
        harness.worker.set_paused(True)
        time.sleep(0.2)
        before = harness.grabber.calls
        time.sleep(1.2)
        self.assertEqual(harness.grabber.calls, before)

    def test_stop_wakes_long_sleep(self):
        harness = WorkerHarness(FakeTranslator(), "Hello", poll=2.0)
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: harness.grabber.calls >= 1))
        started = time.monotonic()
        self.assertTrue(harness.worker.stop(3.0))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_truncation_flag(self):
        long_text = "A" * (engine.MAX_TEXT_CHARS + 500)
        harness = WorkerHarness(FakeTranslator(), long_text)
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: len(harness.results) >= 1))
        generation, text, truncated = harness.results[0]
        self.assertTrue(truncated)
        self.assertEqual(len(text), len("TR:") + engine.MAX_TEXT_CHARS)


class BlockingOcr:
    """OCR çağrısını bir olay açılana kadar bloke eden sahte okuyucu."""

    def __init__(self, text="Hello"):
        self.text = text
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, image, command, lang):
        self.calls += 1
        self.started.set()
        self.release.wait(5.0)
        return self.text


class GenerationRedisplayTests(unittest.TestCase):
    """Nesil değişince aynı metnin yeniden gösterilmesi (tekrar seçim)."""

    def test_reselect_same_text_new_generation_redisplays(self):
        translator = FakeTranslator()
        harness = WorkerHarness(translator, "Hello")
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: len(harness.results) >= 1))
        first_gen = harness.results[0][0]
        # Aynı ROI yeniden seçilir: nesil artar, metin aynı kalır.
        harness.worker.set_roi(Roi(0, 0, 200, 100))
        self.assertTrue(
            wait_until(
                lambda: len([r for r in harness.results if r[1] == "TR:Hello"]) >= 2
            )
        )
        self.assertNotEqual(harness.results[-1][0], first_gen)
        self.assertEqual(translator.calls, ["Hello"])

    def test_pause_resume_same_text_redisplays(self):
        translator = FakeTranslator()
        harness = WorkerHarness(translator, "Hello")
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: len(harness.results) >= 1))
        harness.worker.set_paused(True)
        time.sleep(0.2)
        harness.worker.set_paused(False)
        self.assertTrue(
            wait_until(
                lambda: len([r for r in harness.results if r[1] == "TR:Hello"]) >= 2
            )
        )
        self.assertEqual(translator.calls, ["Hello"])

    def test_blank_after_generation_change_emits_clear(self):
        translator = FakeTranslator()
        harness = WorkerHarness(translator, "Hello")
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: len(harness.results) >= 1))
        harness.ocr.text = ""
        self.assertTrue(wait_until(lambda: any(r[1] == "" for r in harness.results)))
        harness.worker.set_roi(Roi(7, 7, 200, 100))
        new_gen = harness.worker.snapshot().generation
        self.assertTrue(
            wait_until(
                lambda: any(r[0] == new_gen and r[1] == "" for r in harness.results)
            )
        )


class ShutdownRaceTests(unittest.TestCase):
    """Durdurma/duraklatma OCR sırasında gerçekleşirse ağ isteği olmamalı."""

    def _harness(self, ocr, translator):
        results, errors = [], []
        worker = TranslationWorker(
            lambda gen, text, trunc: results.append((gen, text, trunc)),
            lambda _msg: None,
            errors.append,
            poll_interval=0.5,
            grabber=FakeGrabber(),
            ocr_runner=ocr,
            translator_factory=lambda: translator,
        )
        patch = mock.patch("engine.validate_ocr_setup", lambda *a, **k: None)
        patch.start()
        self.addCleanup(patch.stop)
        return worker, results, errors

    def test_stop_during_blocked_ocr_sends_no_text(self):
        translator = FakeTranslator()
        ocr = BlockingOcr()
        worker, results, _errors = self._harness(ocr, translator)
        worker.set_roi(Roi(0, 0, 200, 100))
        worker.set_paused(False)
        worker.start()
        self.assertTrue(ocr.started.wait(5.0))
        worker.stop(timeout=0.0)
        ocr.release.set()
        self.assertTrue(worker.join(5.0))
        self.assertEqual(translator.calls, [])
        self.assertEqual(results, [])

    def test_pause_during_blocked_ocr_sends_no_text(self):
        translator = FakeTranslator()
        ocr = BlockingOcr()
        worker, results, _errors = self._harness(ocr, translator)
        worker.set_roi(Roi(0, 0, 200, 100))
        worker.set_paused(False)
        worker.start()
        self.assertTrue(ocr.started.wait(5.0))
        worker.set_paused(True)
        ocr.release.set()
        time.sleep(0.6)
        self.assertEqual(translator.calls, [])
        self.assertEqual(results, [])
        self.assertTrue(worker.stop(5.0))


class SpacingTests(unittest.TestCase):
    """Aralık değişikliği en az çeviri aralığını aşmamalı."""

    def test_interval_change_does_not_shorten_spacing(self):
        translator = FakeTranslator()
        harness = WorkerHarness(translator, "Hello")
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: len(translator.calls) >= 1))
        # Metni sürekli değiştir ki çeviri art arda denenmek zorunda kalsın.
        for index in range(30):
            harness.ocr.text = f"metin {index}"
            harness.worker.set_poll_interval(0.5 if index % 2 else 2.0)
            time.sleep(0.05)
        self.assertGreaterEqual(len(translator.times), 2)
        gaps = [
            b - a for a, b in zip(translator.times, translator.times[1:])
        ]
        self.assertTrue(
            all(gap >= engine.MIN_TRANSLATE_SPACING - 0.05 for gap in gaps),
            f"aralık ihlali: {gaps}",
        )


class SyncFailTranslator:
    """translate çağrısı senkron hata veren sahte istemci."""

    def __init__(self):
        self.calls = 0

    def translate(self, text, src="en", dest="tr"):
        self.calls += 1
        raise RuntimeError("senkron hata")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ResilienceTests(unittest.TestCase):
    def test_sync_translate_error_keeps_worker_alive(self):
        translator = SyncFailTranslator()
        harness = WorkerHarness(translator, "Hello")
        self.addCleanup(harness.close)
        harness.start_roi()
        self.assertTrue(wait_until(lambda: translator.calls >= 1))
        self.assertTrue(harness.errors)
        self.assertTrue(harness.worker.is_running())

    def test_ocr_validation_retry_is_throttled(self):
        calls = []

        def failing(command, lang):
            calls.append(1)
            raise engine.OcrSetupError("Tesseract yok")

        worker, results, statuses, errors, grabber, ocr = make_worker(FakeTranslator())
        with mock.patch("engine.validate_ocr_setup", failing):
            worker.start()
            worker.set_roi(Roi(0, 0, 200, 100))
            worker.set_paused(False)
            time.sleep(1.6)
            worker.stop(3.0)
        self.assertGreaterEqual(len(calls), 1)
        self.assertLessEqual(len(calls), 2)
        self.assertTrue(errors)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
