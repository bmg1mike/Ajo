"""Speech to text for voice notes (spec sections 3, 7.4 and 10.2).

Telegram voice notes are OGG/Opus. Two backends, chosen by STT_BACKEND:

- `local`: faster-whisper on the CPU. The model (STT_LOCAL_MODEL, a size such as "small" or a
  path) is downloaded and loaded on the first voice note, not at startup.
- `groq`: Groq's OpenAI-compatible Whisper endpoint (STT_GROQ_MODEL). The audio leaves the
  machine, but the request carries no learner details: a fixed file name, the model, a language
  hint and a fixed prompt.

Both are synchronous, like the LLM client; async adapters reach them through the conversation,
which runs in a worker thread. Audio is never stored.
"""
import io
import logging
import random
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

import httpx

from app.llm.client import RETRYABLE_STATUS, retry_after_seconds
from app.models import Language

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
# Whisper's language hint by learner language. Whisper has no Nigerian Pidgin; it is English-based, so English
# decodes it best. Yoruba learners are left to detection: Whisper's Yoruba is weak, and spec section 18 expects
# them to ask in English as often as not.
WHISPER_LANGUAGE: dict[Language, str | None] = {Language.EN: "en", Language.YO: None, Language.PCM: "en"}
# Fixed context that nudges Whisper towards Biology spelling. It never names the learner.
PROMPT = "A student's question about JAMB Biology."


class TranscriptionError(Exception):
    """The audio could not be transcribed. The message never contains the audio or the transcript."""


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, language: Language) -> str: ...
    def close(self) -> None: ...


class LocalWhisper:
    def __init__(self, model: str, *, loader: Callable[[str], object] | None = None) -> None:
        self._name, self._loader, self._model = model, loader or _load_whisper, None
        self._lock = threading.Lock()

    def transcribe(self, audio: bytes, language: Language) -> str:
        with self._lock:  # one decode at a time: the model is not shared safely across threads
            if self._model is None:
                try:  # the first note downloads and loads the model; a failure is retried on the next note
                    self._model = self._loader(self._name)
                except Exception as error:  # e.g. OSError offline, or a bad STT_LOCAL_MODEL
                    raise TranscriptionError(f"local Whisper model failed to load: {type(error).__name__}") from None
            try:
                segments, _ = self._model.transcribe(
                    io.BytesIO(audio), language=WHISPER_LANGUAGE[Language(language)], beam_size=5,
                    vad_filter=True, condition_on_previous_text=False, initial_prompt=PROMPT)
                return " ".join(segment.text.strip() for segment in segments).strip()
            except Exception as error:  # PyAV and CTranslate2 raise many types for bad audio
                raise TranscriptionError(f"local transcription failed: {type(error).__name__}") from None

    def close(self) -> None:
        pass


def _load_whisper(model: str):
    from faster_whisper import WhisperModel  # imported lazily: it loads CTranslate2 and PyAV

    return WhisperModel(model, device="cpu", compute_type="int8")


class GroqWhisper:
    def __init__(self, *, api_key: str, model: str, url: str = GROQ_TRANSCRIPTIONS_URL, timeout: float = 60.0,
                 max_attempts: int = 3, backoff_seconds: float = 1.0, max_backoff_seconds: float = 20.0,
                 sleep: Callable[[float], None] = time.sleep, transport: httpx.BaseTransport | None = None) -> None:
        self._url, self._model = url, model
        self._max_attempts, self._backoff, self._max_backoff, self._sleep = (
            max_attempts, backoff_seconds, max_backoff_seconds, sleep)
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"Authorization": f"Bearer {api_key}"})

    def transcribe(self, audio: bytes, language: Language) -> str:
        data = {"model": self._model, "prompt": PROMPT, "response_format": "json", "temperature": "0"}
        if hint := WHISPER_LANGUAGE[Language(language)]:
            data["language"] = hint
        for attempt in range(1, self._max_attempts + 1):
            retry_after = None
            try:
                response = self._http.post(self._url, data=data, files={"file": ("voice.ogg", audio, "audio/ogg")})
            except httpx.RequestError as error:  # transport, decoding and other request failures
                problem = f"transport error: {type(error).__name__}"
            else:
                if response.status_code == 200:
                    return _transcript(response)
                if response.status_code not in RETRYABLE_STATUS:
                    raise TranscriptionError(f"Groq transcription failed with HTTP {response.status_code}")
                problem, retry_after = f"HTTP {response.status_code}", retry_after_seconds(response)
            if attempt == self._max_attempts:
                raise TranscriptionError(f"Groq transcription failed after {attempt} attempts ({problem})")
            delay = self._backoff * 2 ** (attempt - 1) * (1 + random.random() / 2)
            self._sleep(min(self._max_backoff, max(delay, retry_after or 0.0)))
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._http.close()


def _transcript(response: httpx.Response) -> str:
    try:
        text = response.json()["text"]
    except (ValueError, KeyError, TypeError):
        raise TranscriptionError("Groq response did not match the transcription format") from None
    if not isinstance(text, str):
        raise TranscriptionError("Groq transcript is not text")
    return text.strip()


def from_settings(settings: "Settings") -> Transcriber | None:
    """The configured transcriber, or None when STT is off."""
    if not settings.stt_enabled:
        return None
    if settings.stt_backend == "groq":
        return GroqWhisper(api_key=settings.groq_api_key.get_secret_value(), model=settings.stt_groq_model)
    return LocalWhisper(settings.stt_local_model)
