"""Voice notes in and lesson audio out (spec sections 7.2, 7.4, 10.2 and 11).

A voice note from an onboarded learner is transcribed (app/speech/stt.py) and the transcript goes
to QA like typed text, echoed back first so the learner sees what was heard. Nothing is fetched or
transcribed before a learner exists, so no audio is processed before consent, and audio is never
stored. Both run outside the conversation's transaction, because they take seconds.

The lesson's Listen button reads the lesson and its worked example aloud (app/speech/tts.py). The
learner hears their own language only when its voice is listed in tts.AUDIO_REVIEWED, has a model
and the lesson has a translation; otherwise, or if that synthesis fails, they hear English with a
note. Citations stay on screen and are not read.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.i18n import t
from app.core.lessons import LISTEN
from app.core.replies import Reply
from app.models import CircleMember, Language, Learner, Lesson
from app.speech import tts
from app.speech.stt import Transcriber, TranscriptionError

logger = logging.getLogger(__name__)

# Opus voice notes run at roughly 2 to 4 KB a second; this allows 16 KB a second before refusing.
MAX_BYTES_PER_SECOND = 16_000


@dataclass(frozen=True)
class VoiceNote:
    """An incoming voice note. `fetch` downloads it, and is called only when it will be transcribed."""
    duration_seconds: int
    fetch: Callable[[], bytes] = field(repr=False)
    size_bytes: int | None = None


class VoiceRejected(Exception):
    """A voice note that will not be transcribed; `key` is the i18n string that tells the learner why."""

    def __init__(self, key: str, **values: object) -> None:
        super().__init__(key)
        self.key, self.values = key, values

    def reply(self, language: Language) -> Reply:
        return Reply(t(self.key, language, **self.values))


class VoiceService:
    def __init__(self, sessions: sessionmaker[Session], *, transcriber: Transcriber | None = None,
                 speaker: tts.Speaker | None = None, max_seconds: int = 60) -> None:
        self.sessions, self.transcriber, self.speaker, self.max_seconds = sessions, transcriber, speaker, max_seconds

    @property
    def listening(self) -> bool:
        return self.transcriber is not None

    def transcribe(self, note: VoiceNote, language: Language) -> str:
        """The note's words as one line, or VoiceRejected. Logs failure types only, never audio or text."""
        if self.transcriber is None:
            raise VoiceRejected("voice.unavailable")
        max_bytes = self.max_seconds * MAX_BYTES_PER_SECOND
        if note.duration_seconds > self.max_seconds or (note.size_bytes or 0) > max_bytes:
            raise VoiceRejected("voice.too_long", seconds=self.max_seconds)
        try:
            audio = note.fetch()
        except Exception as error:  # noqa: BLE001 - any download failure gets the same answer
            logger.warning("Voice note download failed (%s)", type(error).__name__)
            raise VoiceRejected("voice.failed") from None
        if len(audio) > max_bytes:
            raise VoiceRejected("voice.too_long", seconds=self.max_seconds)
        if not audio:
            raise VoiceRejected("voice.failed")
        try:
            text = self.transcriber.transcribe(audio, language)
        except TranscriptionError as error:
            logger.warning("Voice note transcription failed: %s", error)
            raise VoiceRejected("voice.failed") from None
        text = " ".join(text.split())
        if not text:
            raise VoiceRejected("voice.empty")
        return text

    def listen(self, user_hash: str, payload: str) -> list[Reply]:
        """The Listen button: the lesson as a voice note, for a member of the lesson's circle."""
        with self.sessions() as session:
            learner = session.scalar(select(Learner).where(Learner.channel_user_hash == user_hash))
            if learner is None:
                return [Reply(t("onboarding.start_first"))]
            language = Language(learner.language)
            if self.speaker is None:
                return [Reply(t("listen.unavailable", language))]
            try:
                lesson = session.get(Lesson, UUID(payload.removeprefix(LISTEN)))
            except ValueError:
                lesson = None
            if lesson is None or session.get(CircleMember, (lesson.circle_id, learner.id)) is None:
                return [Reply(t("button.expired", language))]
            scripts = []
            if (language != Language.EN and language in tts.AUDIO_REVIEWED and language in self.speaker.languages
                    and language.value in lesson.body):
                scripts.append((language, tts.lesson_script(lesson.body[language.value])))
            scripts.append((Language.EN, tts.lesson_script(lesson.body[Language.EN.value])))
        # No transaction is open: synthesis takes seconds, and the speech cache writes in its own.
        for spoken, script in scripts:
            try:
                audio = self.speaker.speak(script, spoken)
            except tts.SpeechError as error:
                logger.warning("Lesson audio failed: %s", error)
                continue
            return [Reply("" if spoken == language else t("listen.english_only", language), voice=audio)]
        return [Reply(t("listen.unavailable", language))]

    def close(self) -> None:
        if self.transcriber is not None:
            self.transcriber.close()
