"""OCR + çeviri motoru.

Bu modül Qt'den bağımsızdır: GUI yalnızca geri çağırma (callback) köprüsü
üzerinden beslenir. Böylece motor sahte OCR/çeviri ile test edilebilir.

Akış tek bir worker thread içinde seri olarak yürür:
ekran görüntüsü -> ön işleme -> OCR -> normalizasyon -> çeviri -> sonuç.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import mss
import pytesseract
from googletrans import Translator
from PIL import Image, ImageEnhance, ImageOps

# --- Ayarlanabilir sınırlar -------------------------------------------------
POLL_DEFAULT = 0.75          # varsayılan tarama aralığı (saniye)
POLL_MIN = 0.5               # arayüzden izin verilen en kısa aralık
POLL_MAX = 2.0               # arayüzden izin verilen en uzun aralık
MIN_TRANSLATE_SPACING = 1.0  # iki çeviri isteği arası en az süre (saniye)
TRANSLATE_TIMEOUT = 8.0      # tek çeviri isteği için toplam üst sınır
OCR_TIMEOUT = 3              # pytesseract image_to_string zaman aşımı
OCR_RETRY_INTERVAL = 5.0     # OCR kurulum hatasını yeniden denemeden önce bekleme
BACKOFF_MAX = 30.0           # üstel geri çekilmenin üst sınırı
CACHE_LIMIT = 128            # LRU çeviri önbelleği kapasitesi
MAX_TEXT_CHARS = 4000        # OCR metni için sert üst sınır
MAX_OCR_SIDE = 2400          # OCR öncesi yeniden boyutlandırma sınırı
OCR_LANG = "eng"
OCR_CONFIG = "--oem 3 --psm 6"
TESSERACT_ENV_VAR = "TESSERACT_CMD"


@dataclass(frozen=True)
class Roi:
    """Fiziksel ekran pikseli cinsinden yakalama alanı."""

    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("ROI genişliği ve yüksekliği pozitif olmalıdır")

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def as_mss_region(self) -> dict:
        """mss.grab için bölge sözlüğü."""
        return {
            "left": int(self.left),
            "top": int(self.top),
            "width": int(self.width),
            "height": int(self.height),
        }


@dataclass(frozen=True)
class WorkerState:
    """Worker'ın thread'ler arası paylaşılan durum anlık görüntüsü."""

    roi: Optional[Roi]
    paused: bool
    generation: int


def normalize_text(raw: str) -> str:
    """OCR metnini satır sonlarını koruyarak sadeleştirir."""
    if not raw:
        return ""
    unified = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in unified.split("\n")]
    result: list[str] = []
    blank_pending = False
    for line in lines:
        if line:
            if blank_pending and result:
                result.append("")
            blank_pending = False
            result.append(line)
        else:
            blank_pending = True
    return "\n".join(result).strip()


def preprocess_image(image: Image.Image) -> Image.Image:
    """OCR için gri tonlama, otomatik kontrast ve 1.5 kontrast uygular."""
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    gray = ImageEnhance.Contrast(gray).enhance(1.5)
    longest = max(gray.size)
    if longest > MAX_OCR_SIDE:
        scale = MAX_OCR_SIDE / float(longest)
        new_size = (max(1, int(gray.width * scale)), max(1, int(gray.height * scale)))
        gray = gray.resize(new_size, Image.LANCZOS)
    return gray


class OcrSetupError(RuntimeError):
    """OCR kurulumu (Tesseract yolu veya dil verisi) kullanılamıyor."""


def standard_tesseract_paths() -> tuple[str, ...]:
    """Windows'ta yaygın Tesseract kurulum yolları."""
    candidates = [r"C:\Program Files\Tesseract-OCR\tesseract.exe"]
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        candidates.append(
            str(Path(local_appdata) / "Programs" / "Tesseract-OCR" / "tesseract.exe")
        )
    return tuple(candidates)


def resolve_tesseract_command(explicit: Optional[str] = None) -> Optional[str]:
    """Tesseract komutunu bulur.

    Sıra: açıkça verilen değer -> TESSERACT_CMD -> PATH -> standart yollar.
    Açıkça verilen (parametre veya ortam değişkeni) geçersiz bir yol
    sessizce başka bir yolla değiştirilmez; doğrulama worker'da yapılır.
    """
    if explicit:
        return explicit
    env_value = os.environ.get(TESSERACT_ENV_VAR, "").strip()
    if env_value:
        return env_value
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in standard_tesseract_paths():
        if Path(candidate).is_file():
            return candidate
    return None


def validate_ocr_setup(command: Optional[str], lang: str = OCR_LANG) -> None:
    """Tesseract çalıştırılabilir mi ve dil verisi var mı kontrol eder.

    Hata durumunda Türkçe mesajlı OcrSetupError yükseltir. Bu kontrol
    GUI'de değil, worker thread içinde çağrılır.
    """
    if not command:
        raise OcrSetupError(
            "Tesseract bulunamadı. Kurulum yolunu TESSERACT_CMD ortam "
            "değişkeni ile belirtin (README'ye bakın)."
        )
    if not Path(command).is_file():
        raise OcrSetupError(
            f"Tesseract yolu geçersiz: {command}. TESSERACT_CMD değerini kontrol edin."
        )
    try:
        completed = subprocess.run(
            [command, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OcrSetupError(
            f"Tesseract çalıştırılamadı ({type(exc).__name__}). Yolu kontrol edin."
        ) from exc
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    langs = {line.strip() for line in output.splitlines() if line.strip()}
    if lang not in langs:
        raise OcrSetupError(
            f"Tesseract '{lang}' dil verisi eksik. Kurulumda English verisini seçin."
        )


def run_ocr(image: Image.Image, command: str, lang: str = OCR_LANG) -> str:
    """Ön işlenmiş görüntüyü Tesseract ile okur."""
    pytesseract.pytesseract.tesseract_cmd = command
    return pytesseract.image_to_string(
        image, lang=lang, config=OCR_CONFIG, timeout=OCR_TIMEOUT
    )


def grab_screen(roi: Roi) -> Image.Image:
    """mss örneğini bu çağrının thread'inde oluşturup kapatır."""
    with mss.mss() as sct:
        shot = sct.grab(roi.as_mss_region())
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class TranslationWorker:
    """Yakalama/OCR/çeviri döngüsünü tek bir thread'de yürütür.

    Durum (ROI, duraklatma, nesil) kilit altında tutulur; GUI thread'i
    yalnızca bu küçük metotları çağırır. Sonuçlar nesil numarası ile
    bildirilir, böylece eski sonuçlar arayüze ulaşmaz.
    """

    def __init__(
        self,
        on_result: Callable[[int, str, bool], None],
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
        *,
        poll_interval: float = POLL_DEFAULT,
        ocr_command: Optional[str] = None,
        ocr_lang: str = OCR_LANG,
        grabber: Optional[Callable[[Roi], Image.Image]] = None,
        ocr_runner: Optional[Callable[[Image.Image, str, str], str]] = None,
        translator_factory: Optional[Callable[[], object]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_result = on_result
        self._on_status = on_status
        self._on_error = on_error
        self._ocr_command = ocr_command
        self._ocr_lang = ocr_lang
        self._grabber = grabber or grab_screen
        self._ocr_runner = ocr_runner or run_ocr
        self._translator_factory = translator_factory or self._default_translator
        self._clock = clock

        self._lock = threading.Lock()
        self._roi: Optional[Roi] = None
        self._paused = True
        self._generation = 0
        self._poll_interval = self._clamp_interval(poll_interval)

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._async_wake: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        self._last_text = ""
        self._last_translation: Optional[str] = None
        self._last_text_generation = -1
        self._last_emitted_generation = -1
        self._cache: "OrderedDict[str, str]" = OrderedDict()
        self._backoff = 0.0
        self._last_attempt = 0.0
        self._validated_generation = -1
        self._ocr_error: Optional[str] = None
        self._ocr_retry_at = 0.0
        self._last_error = ""
        self._last_error_at = 0.0

    # --- Durum ve kontrol ---------------------------------------------------
    @staticmethod
    def _clamp_interval(value: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return POLL_DEFAULT
        return max(POLL_MIN, min(POLL_MAX, numeric))

    def snapshot(self) -> WorkerState:
        with self._lock:
            return WorkerState(self._roi, self._paused, self._generation)

    def set_roi(self, roi: Optional[Roi]) -> None:
        with self._lock:
            self._roi = roi
            self._generation += 1
        self._signal_wake()

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            if self._paused == paused:
                return
            self._paused = paused
            self._generation += 1
        self._signal_wake()

    def set_poll_interval(self, seconds: float) -> None:
        with self._lock:
            self._poll_interval = self._clamp_interval(seconds)
        self._signal_wake()

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ceviri-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 12.0) -> bool:
        self._stop.set()
        self._signal_wake()
        return self.join(timeout)

    def join(self, timeout: float = 12.0) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _signal_wake(self) -> None:
        """Bekleyen uyku/istekleri uyandırır (thread-safe)."""
        self._wake.set()
        loop = self._loop
        event = self._async_wake
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    # --- Thread ve olay döngüsü --------------------------------------------
    def _run(self) -> None:
        """Worker thread girişi: kalıcı bir asyncio döngüsü kurar."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:  # thread sessizce ölmesin
            self._report_error(
                f"Motor beklenmeyen bir hata ile durdu ({type(exc).__name__}).",
                force=True,
            )
        finally:
            self._loop = None
            self._async_wake = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
                # to_thread işleri (OCR) döngü kapanmadan tamamlanmalı.
                loop.run_until_complete(loop.shutdown_default_executor())
            finally:
                loop.close()

    async def _main(self) -> None:
        self._async_wake = asyncio.Event()
        translator_cm = self._translator_factory()
        translator = None
        try:
            translator = await translator_cm.__aenter__()
        except Exception as exc:
            self._report_error(
                f"Çeviri istemcisi başlatılamadı ({type(exc).__name__}).", force=True
            )
        try:
            await self._loop_body(translator)
        finally:
            if translator is not None:
                with contextlib.suppress(Exception):
                    await translator_cm.__aexit__(None, None, None)

    async def _loop_body(self, translator) -> None:
        """Seri yakalama/OCR/çeviri döngüsü."""
        while not self._stop.is_set():
            state = self.snapshot()
            if state.paused or state.roi is None:
                await self._sleep(self._poll_interval)
                continue
            if not await self._ensure_ocr(state.generation):
                await self._sleep(self._poll_interval)
                continue
            raw = await self._capture_and_ocr(state.roi)
            if raw is None:
                await self._sleep(self._poll_interval)
                continue
            # OCR sonrası: durum değiştiyse eski ekran metni ile devam etme.
            if not self._is_current(state):
                continue
            text = normalize_text(raw)
            if not text:
                self._handle_blank(state.generation)
                await self._sleep(self._poll_interval)
                continue
            truncated = len(text) > MAX_TEXT_CHARS
            if truncated:
                text = text[:MAX_TEXT_CHARS]
            # Nesil değiştiyse (yeniden seçim/duraklatma) tekrar gösterim
            # engellenmemeli; LRU önbelleği korunur, yalnızca dedupe sıfırlanır.
            if state.generation != self._last_text_generation:
                self._last_text = ""
                self._last_translation = None
                self._last_text_generation = state.generation
            if text == self._last_text and self._last_translation is not None:
                await self._sleep(self._poll_interval)
                continue
            cached = self._cache_lookup(text)
            if cached is not None:
                self._last_text = text
                self._last_translation = cached
                self._last_text_generation = state.generation
                self._backoff = 0.0
                self._emit_result(state.generation, cached, truncated)
                await self._sleep(self._poll_interval)
                continue
            if translator is None:
                await self._sleep(self._poll_interval)
                continue
            # En az çeviri aralığı kesilebilir uyku ile korunur; aralık
            # değişikliği bekleme süresini kısaltamaz.
            while self._is_current(state):
                remaining = self._spacing_wait()
                if remaining <= 0:
                    break
                await self._sleep(remaining)
            # Ağ isteğinden hemen önce tekrar kontrol: bayat metin gönderilmez.
            if not self._is_current(state):
                continue
            self._last_attempt = self._clock()
            outcome, value = await self._translate(translator, text)
            if outcome == "interrupted":
                continue
            if outcome == "error":
                self._report_error(
                    "Çeviri isteği başarısız oldu; son başarılı çeviri korunuyor."
                )
                self._backoff = min(BACKOFF_MAX, max(1.0, self._backoff * 2))
                await self._sleep(self._backoff)
                continue
            if not self._is_current(state):
                continue
            self._store(text, value, state.generation)
            self._backoff = 0.0
            self._emit_result(state.generation, value, truncated)
            await self._sleep(self._poll_interval)

    # --- Yardımcılar --------------------------------------------------------
    async def _sleep(self, seconds: float) -> None:
        """Duraklatma/ROI değişikliği ile kesilebilen bekleme."""
        self._wake.clear()
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        event = self._async_wake
        if event is None:
            await asyncio.sleep(min(seconds, 0.05))
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=seconds)
        event.clear()

    def _spacing_wait(self) -> float:
        """İki çeviri isteği arasındaki en az süreye kalan bekleme."""
        return max(0.0, MIN_TRANSLATE_SPACING - (self._clock() - self._last_attempt))

    def _is_current(self, state: WorkerState) -> bool:
        if self._stop.is_set():
            return False
        with self._lock:
            return (
                self._generation == state.generation
                and self._roi == state.roi
                and self._paused == state.paused
            )

    async def _translate(self, translator, text: str) -> tuple[str, str]:
        """Tek çeviri isteği; toplam 8 sn sınırlı ve kesilebilir."""
        event = self._async_wake
        if event is None:
            return ("error", "")
        try:
            task = asyncio.ensure_future(
                translator.translate(text, src="en", dest="tr")
            )
        except Exception:
            # Senkron hata (istemci kapalı, geçersiz argüman) worker'ı öldürmesin.
            return ("error", "")
        wake = asyncio.ensure_future(event.wait())
        try:
            done, _ = await asyncio.wait_for(
                asyncio.wait({task, wake}, return_when=asyncio.FIRST_COMPLETED),
                timeout=TRANSLATE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            task.cancel()
            wake.cancel()
            for pending in (task, wake):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending
            return ("error", "")
        if task in done:
            wake.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await wake
            try:
                result = task.result()
            except Exception:
                return ("error", "")
            return ("ok", str(getattr(result, "text", "") or ""))
        interrupted = wake.done()
        task.cancel()
        wake.cancel()
        for pending in (task, wake):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        return ("interrupted", "") if interrupted else ("error", "")

    async def _capture_and_ocr(self, roi: Roi) -> Optional[str]:
        """Yakalama + ön işleme + OCR; hata halinde None döndürür."""
        try:
            image = await asyncio.to_thread(self._grabber, roi)
            prepared = preprocess_image(image)
            return await asyncio.to_thread(
                self._ocr_runner, prepared, self._ocr_command or "", self._ocr_lang
            )
        except Exception as exc:
            self._report_error(f"Ekran okunamadı ({type(exc).__name__}).")
            return None

    async def _ensure_ocr(self, generation: int) -> bool:
        """Tesseract kurulumunu worker içinde doğrular (GUI'de değil)."""
        if self._validated_generation == generation and self._ocr_error is None:
            return True
        if self._ocr_error is not None and self._clock() < self._ocr_retry_at:
            return False
        command = resolve_tesseract_command(self._ocr_command)
        self._ocr_command = command
        try:
            await asyncio.to_thread(validate_ocr_setup, command, self._ocr_lang)
        except OcrSetupError as exc:
            self._ocr_error = str(exc)
            self._validated_generation = generation
            self._ocr_retry_at = self._clock() + OCR_RETRY_INTERVAL
            self._report_error(str(exc), force=True)
            return False
        self._ocr_error = None
        self._validated_generation = generation
        return True

    def _handle_blank(self, generation: int) -> None:
        """Boş OCR: gösterimi temizle, önceki metni sıfırla."""
        if (
            not self._last_text
            and self._last_translation is None
            and self._last_emitted_generation == generation
        ):
            return
        self._last_text = ""
        self._last_translation = None
        self._last_text_generation = generation
        self._emit_result(generation, "", False)

    def _cache_lookup(self, text: str) -> Optional[str]:
        value = self._cache.get(text)
        if value is None:
            return None
        self._cache.move_to_end(text)
        return value

    def _store(self, text: str, translation: str, generation: int) -> None:
        self._last_text = text
        self._last_translation = translation
        self._last_text_generation = generation
        self._cache[text] = translation
        self._cache.move_to_end(text)
        while len(self._cache) > CACHE_LIMIT:
            self._cache.popitem(last=False)

    def _emit_result(self, generation: int, text: str, truncated: bool) -> None:
        self._last_emitted_generation = generation
        with contextlib.suppress(Exception):
            self._on_result(generation, text, truncated)

    def _report_error(self, message: str, force: bool = False) -> None:
        """Aynı hata mesajını 5 saniyede birden fazla kez bildirmez."""
        now = self._clock()
        if not force and message == self._last_error and now - self._last_error_at < 5.0:
            return
        self._last_error = message
        self._last_error_at = now
        with contextlib.suppress(Exception):
            self._on_error(message)

    @staticmethod
    def _default_translator():
        """googletrans asenkron istemcisi (async context manager)."""
        return Translator(raise_exception=True)
