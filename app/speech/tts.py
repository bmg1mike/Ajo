"""Text to speech for the lesson Listen button (spec sections 3, 10.2 and 11).

Meta's MMS-TTS models (VITS, via Hugging Face transformers) turn lesson text into a WAV waveform,
and ffmpeg encodes it as OGG/Opus, the format Telegram plays as a voice note. Models load on the
first request, one per language, so startup never loads torch. Synthesis is seeded, so the same
text always gives the same audio, and the encoded audio is cached in `speech_cache`.

A learner hears their own language only once a native speaker has listened to that voice and it
is listed in AUDIO_REVIEWED; everyone else gets English audio with a note (spec section 11). Make
review files for a listener with:

    uv run python -m app.speech.tts --review yo --out review-yo

The MMS models are CC BY-NC 4.0, which suits this non-commercial proof of concept only.
"""
import argparse
import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
import wave
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import Clock
from app.db import dialect_insert
from app.models import Language, PregeneratedLesson, SpeechCache, SyllabusTopic

logger = logging.getLogger(__name__)

# Languages whose MMS voice a native speaker has listened to and approved for lessons. Add one here,
# with the reviewer and date in the comment, only after its review files have been signed off.
# English is the fallback and needs no sign-off.
AUDIO_REVIEWED: frozenset[Language] = frozenset({Language.EN})

MAX_CHUNK_CHARS = 300  # VITS memory grows with input length; sentences are synthesized in groups this size
PAUSE_SECONDS = 0.3  # between groups, so sentence breaks stay audible
SEED = 0  # VITS samples noise; a fixed seed makes the same text sound the same every time
FORMAT = "ogg-opus-32k-mono-voip-v1"  # part of the cache key: change it when encoding changes
OPUS_BITRATE = "32k"
FFMPEG_TIMEOUT_SECONDS = 60

_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+|\n+")
_DIGITS = re.compile(r"\d+(?:\.\d+)?")
_LETTER_DIGIT = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


class SpeechError(Exception):
    """Audio could not be produced. The message never contains the text being spoken."""


def number_words(value: int) -> str:
    """English words for 0 to 999,999,999; larger numbers are read digit by digit."""
    if value < 20:
        return _ONES[value]
    if value < 100:
        return _TENS[value // 10] + ("" if value % 10 == 0 else " " + _ONES[value % 10])
    for size, name in ((10**6, "million"), (1000, "thousand"), (100, "hundred")):
        if size <= value < size * 1000:
            head, rest = divmod(value, size)
            return f"{number_words(head)} {name}" + ("" if rest == 0 else " " + number_words(rest))
    return " ".join(_ONES[int(digit)] for digit in str(value))


def _english_number(match: re.Match) -> str:
    whole, _, fraction = match.group().partition(".")
    words = number_words(int(whole))
    if fraction:
        words += " point " + " ".join(_ONES[int(digit)] for digit in fraction)
    return f" {words} "


def speakable(text: str, language: Language) -> list[str]:
    """Lesson text as groups of whole sentences an MMS voice can read.

    The English and Pidgin voices know only unaccented Latin letters, and the English one only the
    digits 0 to 6, so numbers become English words and accents are dropped. The Yoruba voice reads
    tone marks and dotted letters, which survive only in composed (NFC) form, but has no digits
    or c, q, v, x and z: those are left for its tokenizer to drop, and the listening check judges
    whether that is acceptable.
    """
    text = unicodedata.normalize("NFC", text).replace("%", " percent ")
    if Language(language) != Language.YO:
        text = _DIGITS.sub(_english_number, _LETTER_DIGIT.sub(" ", text))
        text = unicodedata.normalize("NFC", "".join(char for char in unicodedata.normalize("NFD", text)
                                                    if unicodedata.category(char) != "Mn"))
    groups: list[str] = []
    for sentence in (" ".join(part.split()) for part in _SENTENCE_END.split(text)):
        for piece in _split_long(sentence):
            if groups and len(groups[-1]) + 1 + len(piece) <= MAX_CHUNK_CHARS:
                groups[-1] += " " + piece
            elif piece:
                groups.append(piece)
    return groups


def _split_long(sentence: str) -> list[str]:
    pieces, current = [], ""
    for word in sentence.split(" "):
        if current and len(current) + 1 + len(word) > MAX_CHUNK_CHARS:
            pieces.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    return [*pieces, current] if current else pieces


def lesson_script(body: Mapping[str, str]) -> str:
    """What Listen reads: the lesson, then its worked example. Citations stay on screen."""
    return f"{body['lesson'].strip()}\n{body['example'].strip()}"


# Synthesis


def _load_mms(name: str):
    from transformers import AutoTokenizer, VitsModel  # imported lazily: it loads torch

    return AutoTokenizer.from_pretrained(name), VitsModel.from_pretrained(name).eval()


class MMSSynthesizer:
    """MMS-TTS voices by language, loaded on first use."""

    def __init__(self, models: Mapping[Language, str], *, loader: Callable[[str], tuple] | None = None) -> None:
        self.models = {Language(language): name for language, name in models.items() if name}
        self._loader, self._loaded = loader or _load_mms, {}
        self._lock = threading.Lock()

    @property
    def languages(self) -> frozenset[Language]:
        return frozenset(self.models)

    def synthesize(self, groups: Sequence[str], language: Language) -> bytes:
        """A mono 16-bit WAV of the groups read in order, with a short pause between them."""
        import numpy as np
        import torch

        language = Language(language)
        if language not in self.models:
            raise SpeechError(f"no TTS model is configured for {language.value}")
        with self._lock:  # one synthesis at a time: the seed is global torch state
            try:
                if language not in self._loaded:
                    self._loaded[language] = self._loader(self.models[language])
                tokenizer, model = self._loaded[language]
                rate = model.config.sampling_rate
                parts = []
                for group in groups:
                    inputs = tokenizer(group, return_tensors="pt")
                    if inputs["input_ids"].shape[-1] <= 1:  # nothing the voice can read
                        continue
                    torch.manual_seed(SEED)
                    with torch.no_grad():
                        waveform = model(**inputs).waveform[0].cpu().numpy()
                    if parts:
                        parts.append(np.zeros(int(rate * PAUSE_SECONDS), dtype=np.float32))
                    parts.append(waveform)
            except Exception as error:  # a model download, tokenizer or torch failure
                raise SpeechError(f"{language.value} synthesis failed: {type(error).__name__}") from None
        if not parts:
            raise SpeechError("the text has nothing this voice can read")
        samples = (np.clip(np.concatenate(parts), -1.0, 1.0) * 32767).astype("<i2")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(samples.tobytes())
        return buffer.getvalue()


# Encoding


class OpusEncoder:
    """WAV to OGG/Opus with the ffmpeg command line. Checks at construction that ffmpeg has libopus."""

    def __init__(self, ffmpeg: str = "ffmpeg") -> None:
        path = shutil.which(ffmpeg)
        if path is None:
            raise SpeechError(f"ffmpeg was not found at {ffmpeg!r}: install it or set FFMPEG_PATH")
        try:
            encoders = subprocess.run([path, "-hide_banner", "-encoders"], capture_output=True, text=True,
                                      timeout=FFMPEG_TIMEOUT_SECONDS, check=True).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise SpeechError(f"ffmpeg at {ffmpeg!r} does not run: {type(error).__name__}") from None
        if not re.search(r"^\s*A\S*\s+libopus\b", encoders, re.MULTILINE):
            raise SpeechError(f"ffmpeg at {ffmpeg!r} has no libopus encoder")
        self.path = path

    def encode(self, wav: bytes) -> bytes:
        command = [self.path, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
                   "-c:a", "libopus", "-b:a", OPUS_BITRATE, "-ac", "1", "-application", "voip", "-f", "ogg", "pipe:1"]
        try:
            result = subprocess.run(command, input=wav, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as error:
            raise SpeechError(f"ffmpeg failed: {type(error).__name__}") from None
        if result.returncode != 0 or not result.stdout.startswith(b"OggS"):
            raise SpeechError(f"ffmpeg failed with exit code {result.returncode}")
        return result.stdout


# Speaking with a cache


class Speaker:
    """Text in, cached OGG/Opus out. Call it outside any open transaction: the cache writes in its own."""

    def __init__(self, synthesizer: MMSSynthesizer, encoder: OpusEncoder, sessions: sessionmaker[Session],
                 clock: Clock) -> None:
        self.synthesizer, self.encoder, self.sessions, self.clock = synthesizer, encoder, sessions, clock

    @property
    def languages(self) -> frozenset[Language]:
        return self.synthesizer.languages

    def cache_key(self, groups: Sequence[str], language: Language) -> str:
        material = json.dumps({"model": self.synthesizer.models.get(Language(language)), "language": language,
                               "text": list(groups), "seed": SEED, "format": FORMAT},
                              sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def speak(self, text: str, language: Language) -> bytes:
        language = Language(language)
        if language not in self.languages:
            raise SpeechError(f"no TTS model is configured for {language.value}")
        groups = speakable(text, language)
        if not groups:
            raise SpeechError("there is no text to speak")
        key = self.cache_key(groups, language)
        with self.sessions() as session:
            cached = session.get(SpeechCache, key)
            if cached is not None:
                return cached.audio
        audio = self.encoder.encode(self.synthesizer.synthesize(groups, language))
        with self.sessions.begin() as session:
            session.execute(dialect_insert(session, SpeechCache).values(
                audio_hash=key, audio=audio, created_at=self.clock.now(session),
            ).on_conflict_do_nothing(index_elements=["audio_hash"]))
        return audio


def from_settings(settings, sessions: sessionmaker[Session], clock: Clock) -> Speaker | None:
    """The configured speaker, or None when TTS is off. Fails fast if ffmpeg cannot encode Opus."""
    if not settings.tts_enabled:
        return None
    models = {Language(code): name for code, name in settings.tts_models.items()}
    return Speaker(MMSSynthesizer(models), OpusEncoder(settings.ffmpeg_path), sessions, clock)


# Native-speaker listening check


def review_files(session: Session, speaker: Speaker, language: Language, out: Path) -> list[str]:
    """Write each pre-generated lesson's audio in `language` and a review sheet; returns problems found."""
    out.mkdir(parents=True, exist_ok=True)
    rows = [f"# Ajo lesson audio: {language.value} listening check", "",
            f"Voice: `{speaker.synthesizer.models[language]}`. Listen to each file beside its text. Check that it "
            "is understandable, that tones and dotted letters sound right, and note anything skipped or "
            "mispronounced (numbers and the letters c, q, v, x and z may be dropped). When every row is "
            f"acceptable, add `{language.value}` to AUDIO_REVIEWED in app/speech/tts.py with your name and the date.",
            "", "| File | Topic | Text | OK / problems |", "| --- | --- | --- | --- |"]
    problems = []
    stored = session.execute(select(PregeneratedLesson, SyllabusTopic)
                             .join(SyllabusTopic, SyllabusTopic.id == PregeneratedLesson.topic_id)
                             .order_by(SyllabusTopic.order_index)).all()
    for lesson, topic in stored:
        translation = lesson.content.get("translations", {}).get(language.value)
        if translation is None:
            problems.append(f"topic {topic.order_index} has no {language.value} translation")
            continue
        text = lesson_script(translation)
        name = f"topic-{topic.order_index:02d}.ogg"
        try:
            (out / name).write_bytes(speaker.speak(text, language))
        except SpeechError as error:
            problems.append(f"topic {topic.order_index}: {error}")
            continue
        cell = text.replace("|", "\\|").replace("\n", "<br>")
        rows.append(f"| `{name}` | {topic.title} | {cell} |  |")
    (out / "review.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    from app.config import Settings
    from app.db import build_engine, session_factory

    parser = argparse.ArgumentParser(description="Write lesson audio and a sheet for a native-speaker listening check.")
    parser.add_argument("--review", required=True, choices=[item.value for item in Language if item != Language.EN])
    parser.add_argument("--out", type=Path, required=True, help="directory for the .ogg files and review.md")
    args = parser.parse_args(argv)
    settings = Settings()
    language = Language(args.review)
    models = {Language(code): name for code, name in settings.tts_models.items()}
    if language not in models:
        print(f"No TTS model is configured for {language.value}", file=sys.stderr)
        return 2
    engine = build_engine(settings.database_url)
    try:
        sessions = session_factory(engine)
        speaker = Speaker(MMSSynthesizer(models), OpusEncoder(settings.ffmpeg_path), sessions, Clock(sessions))
        with sessions() as session:
            problems = review_files(session, speaker, language, args.out)
    finally:
        engine.dispose()
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"Wrote {args.out / 'review.md'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
