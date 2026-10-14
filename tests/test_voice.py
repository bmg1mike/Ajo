"""Milestone 8: voice notes in (STT) and lesson audio out (TTS), with fake engines and synthetic learners.

Real models and ffmpeg are exercised only with AJO_SPEECH_TESTS=1 (`-m speech`), since they download
several hundred MB; the ffmpeg encoder test also runs whenever an ffmpeg with libopus is found.
"""
import asyncio
import io
import json
import os
import shutil
import stat
import time
import wave
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from telegram import Bot, Update

from app.adapters.telegram_bot import TelegramAdapter, inbound_from_update, telegram_polling
from app.config import Settings
from app.core import lessons, onboarding, qa
from app.core.conversation import Conversation, Inbound
from app.core.i18n import t
from app.core.lessons import LessonService
from app.core.privacy import channel_hash, delete_learner
from app.core.qa import QAService
from app.core.replies import Reply
from app.core.voice import VoiceNote, VoiceService
from app.main import create_app
from app.models import Channel, Chunk, CircleMember, Language, Learner, Lesson, QALog, SpeechCache, SyllabusTopic
from app.speech import stt, tts
from app.speech.stt import GroqWhisper, LocalWhisper, TranscriptionError
from app.speech.tts import MMSSynthesizer, OpusEncoder, Speaker, SpeechError, lesson_script, number_words, speakable
from tests.fakes import FakeEmbedder, FakeLLM, add_goal_with_chunks, chat
from tests.test_onboarding import KIDNEY, LEAF, LEAF_QUESTION, SALT, User
from tests.test_telegram import TOKEN, FakeBotAPI, FakeRequest, _null_context, callback_update, message_update

AUDIO = b"OggS synthetic voice note"
ENGLISH = {"lesson": "Leaves capture light with chlorophyll and make glucose.",
           "example": "A leaf in sunlight makes 6 molecules of glucose."}
YORUBA = {"lesson": "Ewé máa ń lo chlorophyll láti gba ìmọ́lẹ̀.", "example": "Ewé kan nínú oòrùn máa ń ṣe glucose."}


def answer_json(*cited: str) -> str:
    return json.dumps({"answerable": True, "answer": "Leaves capture light with chlorophyll and make sugar.",
                       "cited_chunk_ids": list(cited)})


# Fakes


class FakeTranscriber:
    def __init__(self, *results: str | Exception) -> None:
        self.results, self.calls = list(results), []

    def transcribe(self, audio: bytes, language: Language) -> str:
        self.calls.append((audio, Language(language)))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        pass


class FakeSynthesizer:
    """Stands in for MMSSynthesizer: "WAV" bytes naming the language and text; fails for `failing` languages."""

    def __init__(self, languages=(Language.EN, Language.YO, Language.PCM), failing=()) -> None:
        self.models = {Language(language): f"facebook/mms-tts-{language}" for language in languages}
        self.failing, self.calls = set(failing), []

    @property
    def languages(self) -> frozenset[Language]:
        return frozenset(self.models)

    def synthesize(self, groups, language) -> bytes:
        self.calls.append((list(groups), Language(language)))
        if language in self.failing:
            raise SpeechError(f"{language} synthesis failed: RuntimeError")
        return f"WAV|{language}|{' '.join(groups)}".encode()


class FakeEncoder:
    def encode(self, wav: bytes) -> bytes:
        return b"OggS" + wav


class Note:
    """A voice note whose download is counted."""

    def __init__(self, data: bytes = AUDIO, *, seconds: int = 5, size: int | None = None, error: bool = False):
        self.data, self.error, self.fetches = data, error, 0
        self.note = VoiceNote(duration_seconds=seconds, fetch=self.fetch, size_bytes=size)

    def fetch(self) -> bytes:
        self.fetches += 1
        if self.error:
            raise RuntimeError("download failed for chat 7012345678")
        return self.data


@pytest.fixture
def goal_id(sessions):
    with sessions.begin() as session:
        goal, _ = add_goal_with_chunks(session, [LEAF, KIDNEY])
        return goal.id


@pytest.fixture
def make_voice_conversation(sessions, clock, make_client):
    def make(*responses, transcriber=None, speaker=None, fake: FakeLLM | None = None, max_seconds: int = 60):
        client = make_client(fake or FakeLLM(*responses))
        voice = VoiceService(sessions, transcriber=transcriber, speaker=speaker, max_seconds=max_seconds)
        return Conversation(sessions, clock, QAService(FakeEmbedder(), client, clock, min_score=0.3),
                            LessonService(FakeEmbedder(), client, clock, audio=speaker is not None),
                            salt=SALT, voice=voice)
    return make


def speak(user: User, note: Note) -> list[Reply]:
    return user.conversation.handle(Inbound(Channel.TELEGRAM, user.user_id, user.chat_id, voice=note.note))


def learner_of(sessions, user: User) -> Learner:
    with sessions() as session:
        return session.scalar(select(Learner).where(Learner.channel_user_hash == user.hash))


# Voice notes in


def test_an_english_voice_question_gets_a_cited_answer(sessions, goal_id, make_voice_conversation):
    fake = FakeLLM(chat(answer_json("C1")), chat("yes"))
    transcriber = FakeTranscriber(f"  {LEAF_QUESTION}\n")
    user = User(make_voice_conversation(fake=fake, transcriber=transcriber))
    user.onboard(goal_id)
    note = Note()
    replies = speak(user, note)
    assert replies[0].text == t("voice.heard", text=LEAF_QUESTION)
    assert "Source: Synthetic Book" in replies[1].text
    assert any(button.payload.startswith(qa.SHOW_SOURCE) for button in replies[1].buttons)
    assert note.fetches == 1 and transcriber.calls == [(AUDIO, Language.EN)]
    with sessions() as session:
        log = session.scalar(select(QALog).where(QALog.learner_id == learner_of(sessions, user).id))
        assert log.question_text == LEAF_QUESTION and log.answered and log.cited_chunk_ids


def test_a_transcript_is_always_a_question_never_a_command(sessions, goal_id, make_voice_conversation):
    transcriber = FakeTranscriber("/delete_me")
    user = User(make_voice_conversation(chat(json.dumps({"answerable": False, "answer": "", "cited_chunk_ids": []})),
                                        transcriber=transcriber))
    user.onboard(goal_id)
    replies = speak(user, Note())
    assert replies[0].text == t("voice.heard", text="/delete_me")
    assert replies[1].text == t("qa.fallback")
    assert learner_of(sessions, user) is not None


def test_spoken_names_and_numbers_are_redacted_before_the_llm(sessions, goal_id, make_voice_conversation):
    fake = FakeLLM(chat(answer_json("C1")), chat("yes"))
    transcriber = FakeTranscriber(f"I am Amaka, call me on 0803 123 4567. {LEAF_QUESTION}")
    user = User(make_voice_conversation(fake=fake, transcriber=transcriber))
    user.onboard(goal_id, name="Amaka")
    speak(user, Note())
    sent = json.dumps(fake.requests, ensure_ascii=False)
    assert "Amaka" not in sent and "4567" not in sent and "leaf cells" in sent


def test_voice_before_onboarding_is_never_downloaded(sessions, goal_id, make_voice_conversation):
    transcriber = FakeTranscriber()
    user = User(make_voice_conversation(transcriber=transcriber))
    note = Note()
    replies = speak(user, note)  # not even started
    assert [reply.text for reply in replies] == [t("voice.after_onboarding"), t("onboarding.start_first")]
    user.send("/start")
    user.tap(onboarding.AGREE)
    replies = speak(user, note)  # agreed, still choosing a language
    assert replies[0].text == t("voice.after_onboarding") and replies[1].buttons
    assert note.fetches == 0 and transcriber.calls == []


@pytest.mark.parametrize("note, key", [
    (Note(seconds=61), "voice.too_long"),
    (Note(size=60 * 16_000 + 1), "voice.too_long"),
])
def test_long_voice_notes_are_refused_before_download(sessions, goal_id, make_voice_conversation, note, key):
    transcriber = FakeTranscriber()
    user = User(make_voice_conversation(transcriber=transcriber))
    user.onboard(goal_id)
    assert [reply.text for reply in speak(user, note)] == [t(key, seconds=60)]
    assert note.fetches == 0 and transcriber.calls == []


@pytest.mark.parametrize("note, results, key", [
    (Note(b"x" * (60 * 16_000 + 1)), [], "voice.too_long"),  # the reported size was missing or wrong
    (Note(error=True), [], "voice.failed"),
    (Note(b""), [], "voice.failed"),
    (Note(), [TranscriptionError("Groq transcription failed with HTTP 400")], "voice.failed"),
    (Note(), [" \n "], "voice.empty"),
])
def test_voice_problems_get_a_plain_answer_and_log_no_details(sessions, goal_id, make_voice_conversation, caplog,
                                                              note, results, key):
    user = User(make_voice_conversation(transcriber=FakeTranscriber(*results)))
    user.onboard(goal_id)
    assert [reply.text for reply in speak(user, note)] == [t(key, seconds=60)]
    assert user.user_id not in caplog.text and "7012345678" not in caplog.text
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(QALog)
                              .where(QALog.learner_id == learner_of(sessions, user).id)) == 0


def test_voice_notes_need_stt(sessions, goal_id, make_voice_conversation):
    user = User(make_voice_conversation())  # STT off
    user.onboard(goal_id)
    note = Note()
    assert [reply.text for reply in speak(user, note)] == [t("voice.unavailable")]
    assert note.fetches == 0
    assert [reply.text for reply in user.send(None)] == [t("message.text_only")]


def test_other_media_get_the_supported_formats_when_stt_is_on(sessions, goal_id, make_voice_conversation):
    user = User(make_voice_conversation(transcriber=FakeTranscriber()))
    user.onboard(goal_id)
    assert [reply.text for reply in user.send(None)] == [t("message.unsupported")]


def test_whisper_gets_a_language_hint_for_the_learner(sessions, goal_id, make_voice_conversation):
    transcriber = FakeTranscriber("Wetin be chlorophyll?")
    user = User(make_voice_conversation(chat(json.dumps({"answerable": False, "answer": "", "cited_chunk_ids": []})),
                                        transcriber=transcriber))
    user.onboard(goal_id, language=Language.PCM)
    speak(user, Note())
    assert transcriber.calls[0][1] == Language.PCM
    assert stt.WHISPER_LANGUAGE == {Language.EN: "en", Language.YO: None, Language.PCM: "en"}


# STT backends


def test_groq_request_carries_no_learner_details_and_retries():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json={"text": " What is osmosis? "})

    slept = []
    groq = GroqWhisper(api_key="gsk-test", model="whisper-large-v3-turbo", sleep=slept.append,
                       transport=httpx.MockTransport(handler))
    assert groq.transcribe(AUDIO, Language.PCM) == "What is osmosis?"
    body = requests[-1].content
    assert b'filename="voice.ogg"' in body and AUDIO in body
    assert b"whisper-large-v3-turbo" in body and b'name="language"\r\n\r\nen' in body
    assert requests[-1].headers["authorization"] == "Bearer gsk-test"
    assert slept and slept[0] >= 2
    groq.transcribe(AUDIO, Language.YO)  # Yoruba learners often ask in English: Whisper detects the language
    assert b'name="language"' not in requests[-1].content
    groq.close()


@pytest.mark.parametrize("response, problem", [
    (httpx.Response(400, json={"error": "bad audio"}), "HTTP 400"),
    (httpx.Response(200, text="not json"), "format"),
    (httpx.Response(200, json={"text": 3}), "not text"),
    (httpx.Response(503), "after 3 attempts"),
])
def test_groq_failures_raise_transcription_errors(response, problem):
    groq = GroqWhisper(api_key="k", model="m", sleep=lambda seconds: None,
                       transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(TranscriptionError, match=problem):
        groq.transcribe(AUDIO, Language.EN)


def test_local_whisper_loads_once_and_wraps_decoder_errors():
    class Segment:
        def __init__(self, text):
            self.text = text

    class Model:
        def __init__(self):
            self.calls = []

        def transcribe(self, audio, **options):
            self.calls.append(options)
            if audio.read() == b"bad":
                raise ValueError("Invalid data found when processing input")
            return iter([Segment(" What is "), Segment("osmosis? ")]), None

    loaded = []
    whisper = LocalWhisper("small", loader=lambda name: loaded.append(name) or Model())
    assert whisper.transcribe(AUDIO, Language.YO) == "What is osmosis?"
    with pytest.raises(TranscriptionError, match="ValueError"):
        whisper.transcribe(b"bad", Language.EN)
    assert loaded == ["small"]


def test_groq_request_errors_are_transcription_errors():
    def handler(request):
        raise httpx.DecodingError("bad gzip", request=request)

    groq = GroqWhisper(api_key="k", model="m", sleep=lambda seconds: None, transport=httpx.MockTransport(handler))
    with pytest.raises(TranscriptionError, match="DecodingError"):
        groq.transcribe(AUDIO, Language.EN)


def test_local_whisper_load_failures_are_transcription_errors_and_retried():
    attempts = []

    def loader(name):
        attempts.append(name)
        if len(attempts) == 1:
            raise OSError("cannot download Systran/faster-whisper-small")
        return type("Model", (), {"transcribe": lambda self, audio, **options: (iter([]), None)})()

    whisper = LocalWhisper("small", loader=loader)
    with pytest.raises(TranscriptionError, match="failed to load: OSError"):
        whisper.transcribe(AUDIO, Language.EN)
    assert whisper.transcribe(AUDIO, Language.EN) == "" and attempts == ["small", "small"]


def test_a_whisper_load_failure_gets_the_learner_reply(sessions, goal_id, make_voice_conversation, caplog):
    def broken(name):
        raise OSError("cannot download Systran/faster-whisper-small")

    user = User(make_voice_conversation(transcriber=LocalWhisper("small", loader=broken)))
    user.onboard(goal_id)
    assert [reply.text for reply in speak(user, Note())] == [t("voice.failed")]
    assert "failed to load: OSError" in caplog.text


def test_stt_and_tts_follow_settings(sessions, clock, tmp_path):
    base = dict(_env_file=None, hash_salt=SALT)
    assert stt.from_settings(Settings(**base)) is None
    assert isinstance(stt.from_settings(Settings(**base, stt_enabled=True)), LocalWhisper)
    groq = stt.from_settings(Settings(**base, stt_enabled=True, stt_backend="groq", groq_api_key="k"))
    assert isinstance(groq, GroqWhisper)
    groq.close()
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        Settings(**base, stt_enabled=True, stt_backend="groq")
    with pytest.raises(ValueError, match="TTS_MODEL_EN"):
        Settings(**base, tts_enabled=True, tts_model_en=" ")
    with pytest.raises(ValueError, match="STT_MAX_SECONDS"):
        Settings(**base, stt_max_seconds=0)
    assert tts.from_settings(Settings(**base), sessions, clock) is None
    with pytest.raises(SpeechError, match="FFMPEG_PATH"):
        tts.from_settings(Settings(**base, tts_enabled=True, ffmpeg_path=str(tmp_path / "missing")), sessions, clock)
    speaker = tts.from_settings(Settings(**base, tts_enabled=True, tts_model_pcm="",
                                         ffmpeg_path=str(fake_ffmpeg(tmp_path))), sessions, clock)
    assert speaker.languages == {Language.EN, Language.YO}


# Text preparation


@pytest.mark.parametrize("value, words", [
    (0, "zero"), (7, "seven"), (19, "nineteen"), (40, "forty"), (46, "forty six"), (100, "one hundred"),
    (365, "three hundred sixty five"), (2026, "two thousand twenty six"), (1_000_000, "one million"),
])
def test_number_words(value, words):
    assert number_words(value) == words


def test_english_text_spells_numbers_and_drops_accents():
    groups = speakable("Photosynthesis uses 6CO2 and 12% of light; café cells divide 3.5 times.", Language.EN)
    assert groups == ["Photosynthesis uses six CO two and twelve percent of light; "
                      "cafe cells divide three point five times."]


def test_yoruba_text_keeps_tone_marks_in_composed_form():
    import unicodedata

    decomposed = unicodedata.normalize("NFD", "Ẹ̀kọ́ òní: ìmúṣẹ́dá oúnjẹ 12")
    (group,) = speakable(decomposed, Language.YO)
    assert group == unicodedata.normalize("NFC", "Ẹ̀kọ́ òní: ìmúṣẹ́dá oúnjẹ 12")
    assert "ẹ" in group.lower() and "ṣ" in group  # the Yoruba voice has these letters only in NFC


def test_long_text_is_grouped_by_sentence_in_order():
    sentences = [f"Sentence number {index} talks about the leaf and its cells." for index in range(1, 30)]
    groups = speakable(" ".join(sentences), Language.EN)
    assert len(groups) > 1 and all(len(group) <= tts.MAX_CHUNK_CHARS for group in groups)
    assert all(group.endswith("cells.") for group in groups)  # groups break between sentences
    assert groups[0].startswith("Sentence number one ") and groups[-1].endswith("twenty nine talks about the leaf "
                                                                               "and its cells.")
    long_run = speakable("word " * 200, Language.EN)  # one sentence too long for a group
    assert len(long_run) > 1 and all(len(group) <= tts.MAX_CHUNK_CHARS for group in long_run)
    assert sum(group.count("word") for group in long_run) == 200
    assert speakable(" \n ", Language.EN) == []


def test_listen_reads_the_lesson_then_the_example():
    assert lesson_script({"lesson": " Leaves make food. ", "example": "A bean leaf."}) == "Leaves make food.\nA bean leaf."


# Synthesis and encoding


class FakeTokenizer:
    def __call__(self, text, return_tensors=None):
        import torch

        letters = [char for char in text if char.isalpha()]
        return {"input_ids": torch.zeros((1, 2 * len(letters) + 1), dtype=torch.long)}


class FakeVits:
    class config:
        sampling_rate = 16_000

    def __call__(self, input_ids):
        import torch

        class Output:
            waveform = torch.rand((1, input_ids.shape[-1] * 100)) * 2 - 1  # depends on the seed

        return Output()


def test_mms_synthesis_is_seeded_padded_and_wav():
    loaded = []
    synth = MMSSynthesizer({Language.EN: "facebook/mms-tts-eng", Language.PCM: ""},
                           loader=lambda name: loaded.append(name) or (FakeTokenizer(), FakeVits()))
    assert synth.languages == {Language.EN}
    first = synth.synthesize(["Leaves make food.", "123", "Roots take water."], Language.EN)
    assert first == synth.synthesize(["Leaves make food.", "123", "Roots take water."], Language.EN)
    assert loaded == ["facebook/mms-tts-eng"]
    with wave.open(io.BytesIO(first)) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16_000)
        # two readable groups ("123" has nothing to read) and one pause between them
        assert audio.getnframes() == 2 * (2 * 14 + 1) * 100 + int(16_000 * tts.PAUSE_SECONDS)
    with pytest.raises(SpeechError, match="nothing this voice can read"):
        synth.synthesize(["42"], Language.EN)
    with pytest.raises(SpeechError, match="no TTS model"):
        synth.synthesize(["Hello"], Language.YO)


def test_mms_model_failures_become_speech_errors():
    def broken(name):
        raise OSError(f"cannot download {name}")

    with pytest.raises(SpeechError, match="en synthesis failed: OSError"):
        MMSSynthesizer({Language.EN: "facebook/mms-tts-eng"}, loader=broken).synthesize(["Hi"], Language.EN)


def fake_ffmpeg(directory: Path, *, opus: bool = True, output: bytes = b"OggS-encoded") -> Path:
    """A stand-in ffmpeg: lists encoders, or writes `output` for any other command."""
    script = directory / "ffmpeg"
    encoders = " A....D libopus              libopus Opus (codec opus)" if opus else " A....D aac  AAC"
    script.write_text("#!/bin/sh\n"
                      'if [ "$2" = "-encoders" ]; then\n'
                      f"  echo 'Encoders:'; echo '{encoders}'; exit 0\nfi\n"
                      f"cat > /dev/null; printf '{output.decode()}'\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_encoder_needs_ffmpeg_with_libopus(tmp_path):
    with pytest.raises(SpeechError, match="was not found"):
        OpusEncoder(str(tmp_path / "nowhere"))
    (tmp_path / "no-opus").mkdir()
    with pytest.raises(SpeechError, match="no libopus"):
        OpusEncoder(str(fake_ffmpeg(tmp_path / "no-opus", opus=False)))
    assert OpusEncoder(str(fake_ffmpeg(tmp_path))).encode(b"RIFF") == b"OggS-encoded"
    (tmp_path / "broken").mkdir()
    with pytest.raises(SpeechError, match="exit code 0"):
        OpusEncoder(str(fake_ffmpeg(tmp_path / "broken", output=b"not ogg"))).encode(b"RIFF")


def real_ffmpeg() -> str | None:
    candidate = os.environ.get("FFMPEG_PATH", "ffmpeg")
    return candidate if shutil.which(candidate) else None


@pytest.mark.skipif(real_ffmpeg() is None, reason="no ffmpeg on PATH or FFMPEG_PATH")
def test_real_ffmpeg_makes_ogg_opus():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x10" * 16_000)
    encoded = OpusEncoder(real_ffmpeg()).encode(buffer.getvalue())
    assert encoded.startswith(b"OggS") and b"OpusHead" in encoded[:200]


def test_speaker_caches_by_model_language_and_text(sessions, clock):
    synth = FakeSynthesizer()
    speaker = Speaker(synth, FakeEncoder(), sessions, clock)
    first = speaker.speak("Leaves make food.", Language.EN)
    assert first == b"OggSWAV|en|Leaves make food."
    assert speaker.speak("Leaves  make food.\n", Language.EN) == first  # same spoken text: served from the cache
    assert len(synth.calls) == 1
    speaker.speak("Leaves make food.", Language.YO)
    assert len(synth.calls) == 2
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(SpeechCache)) == 2
    other_voice = Speaker(FakeSynthesizer(), FakeEncoder(), sessions, clock)
    other_voice.synthesizer.models[Language.EN] = "another/voice"
    other_voice.speak("Leaves make food.", Language.EN)
    assert len(other_voice.synthesizer.calls) == 1  # a different model never reuses the cache
    with pytest.raises(SpeechError, match="no text"):
        speaker.speak(" ", Language.EN)
    with pytest.raises(SpeechError, match="no TTS model"):
        Speaker(FakeSynthesizer([Language.EN]), FakeEncoder(), sessions, clock).speak("Hi", Language.YO)


# The Listen button


def add_lesson(sessions, clock, user: User, body: dict) -> Lesson:
    """Today's lesson for the user's circle. Each gets its own wording, so earlier tests' cached audio never matches."""
    marker = f" Lesson {uuid4().hex[:8]}."
    body = {language: text | {"lesson": text["lesson"] + marker} for language, text in body.items()}
    with sessions.begin() as session:
        learner = session.scalar(select(Learner).where(Learner.channel_user_hash == user.hash))
        member = session.scalar(select(CircleMember).where(CircleMember.learner_id == learner.id))
        chunk = session.scalar(select(Chunk).join(SyllabusTopic).where(SyllabusTopic.goal_id == learner.goal_id))
        lesson = Lesson(circle_id=member.circle_id, day_index=1, topic_id=chunk.syllabus_topic_id, body=body,
                        cited_chunk_ids=[chunk.id], posted_at=clock.now(session))
        session.add(lesson)
        session.flush()
        session.expunge(lesson)
        return lesson


def listen_payload(replies: list[Reply]) -> str:
    (payload,) = [b.payload for reply in replies for b in reply.buttons if b.payload.startswith(lessons.LISTEN)]
    return payload


def test_lesson_offers_listen_only_when_tts_is_on(sessions, clock, goal_id, make_voice_conversation):
    speaker = Speaker(FakeSynthesizer(), FakeEncoder(), sessions, clock)
    user = User(make_voice_conversation(speaker=speaker))
    user.onboard(goal_id)
    lesson = add_lesson(sessions, clock, user, {"en": ENGLISH})
    buttons = user.send("/lesson")[-1].buttons
    labels = [button.text for button in buttons]
    assert labels[-2:] == [t("lesson.listen"), t("lesson.answer_questions")]
    assert buttons[-2].payload == f"{lessons.LISTEN}{lesson.id}" and len(buttons[-2].payload.encode()) <= 64
    plain = User(make_voice_conversation())
    plain.onboard(goal_id)
    assert not any(b.payload.startswith(lessons.LISTEN) for b in plain.send("/lesson")[-1].buttons)


def test_english_learner_hears_the_lesson(sessions, clock, goal_id, make_voice_conversation):
    synth = FakeSynthesizer()
    user = User(make_voice_conversation(speaker=Speaker(synth, FakeEncoder(), sessions, clock)))
    user.onboard(goal_id)
    add_lesson(sessions, clock, user, {"en": ENGLISH})
    (reply,) = user.tap(listen_payload(user.send("/lesson")))
    assert reply.text == "" and reply.voice.startswith(b"OggSWAV|en|Leaves capture light")
    assert "six molecules" in reply.voice.decode()  # numbers spelled out for the English voice
    assert "Source" not in reply.voice.decode()  # citations stay on screen
    (again,) = user.tap(listen_payload(user.send("/lesson")))
    assert again.voice == reply.voice and len(synth.calls) == 1  # cached


@pytest.mark.parametrize("language", [Language.YO, Language.PCM])
def test_unreviewed_local_audio_falls_back_to_english_with_a_note(sessions, clock, goal_id, make_voice_conversation,
                                                                  language):
    synth = FakeSynthesizer()
    user = User(make_voice_conversation(speaker=Speaker(synth, FakeEncoder(), sessions, clock)))
    user.onboard(goal_id, language=language)
    lesson = add_lesson(sessions, clock, user, {"en": ENGLISH, "yo": YORUBA, "pcm": YORUBA})
    (reply,) = user.tap(f"{lessons.LISTEN}{lesson.id}")
    assert tts.AUDIO_REVIEWED == {Language.EN}  # update together with a listening sign-off in tts.py
    assert reply.text == t("listen.english_only", language) and reply.voice.startswith(b"OggSWAV|en|")
    assert [call[1] for call in synth.calls] == [Language.EN]


def test_reviewed_local_audio_is_played_in_the_learner_language(sessions, clock, goal_id, make_voice_conversation,
                                                                monkeypatch):
    monkeypatch.setattr(tts, "AUDIO_REVIEWED", frozenset({Language.EN, Language.YO}))
    synth = FakeSynthesizer()
    user = User(make_voice_conversation(speaker=Speaker(synth, FakeEncoder(), sessions, clock)))
    user.onboard(goal_id, language=Language.YO)
    lesson = add_lesson(sessions, clock, user, {"en": ENGLISH, "yo": YORUBA})
    (reply,) = user.tap(f"{lessons.LISTEN}{lesson.id}")
    assert reply.text == "" and reply.voice == b"OggSWAV|yo|" + " ".join(
        speakable(lesson_script(lesson.body["yo"]), Language.YO)).encode()


@pytest.mark.parametrize("body, synth", [
    ({"en": ENGLISH}, FakeSynthesizer()),  # the lesson has no Yoruba translation
    ({"en": ENGLISH, "yo": YORUBA}, FakeSynthesizer(failing=[Language.YO])),  # the Yoruba voice failed
    ({"en": ENGLISH, "yo": YORUBA}, FakeSynthesizer(languages=[Language.EN])),  # no Yoruba model configured
])
def test_reviewed_language_still_falls_back_when_its_audio_is_unavailable(
        sessions, clock, goal_id, make_voice_conversation, monkeypatch, body, synth):
    monkeypatch.setattr(tts, "AUDIO_REVIEWED", frozenset({Language.EN, Language.YO}))
    user = User(make_voice_conversation(speaker=Speaker(synth, FakeEncoder(), sessions, clock)))
    user.onboard(goal_id, language=Language.YO)
    lesson = add_lesson(sessions, clock, user, body)
    (reply,) = user.tap(f"{lessons.LISTEN}{lesson.id}")
    assert reply.text == t("listen.english_only", Language.YO) and reply.voice.startswith(b"OggSWAV|en|")


def test_listen_says_when_no_audio_can_be_made(sessions, clock, goal_id, make_voice_conversation):
    user = User(make_voice_conversation(speaker=Speaker(FakeSynthesizer(failing=[Language.EN]), FakeEncoder(),
                                                        sessions, clock)))
    user.onboard(goal_id)
    lesson = add_lesson(sessions, clock, user, {"en": ENGLISH})
    assert [(r.text, r.voice) for r in user.tap(f"{lessons.LISTEN}{lesson.id}")] == [(t("listen.unavailable"), None)]
    off = User(make_voice_conversation())  # TTS off, but an old lesson message still has the button
    off.onboard(goal_id)
    assert [r.text for r in off.tap(f"{lessons.LISTEN}{lesson.id}")] == [t("listen.unavailable")]


def test_listen_is_only_for_members_of_the_lesson_circle(sessions, clock, goal_id, make_voice_conversation):
    synth = FakeSynthesizer()
    conversation = make_voice_conversation(speaker=Speaker(synth, FakeEncoder(), sessions, clock))
    member = User(conversation)
    member.onboard(goal_id)
    lesson = add_lesson(sessions, clock, member, {"en": ENGLISH})
    with sessions.begin() as session:
        other_goal, _ = add_goal_with_chunks(session, [LEAF])
        other_goal_id = other_goal.id
    outsider = User(conversation)
    outsider.onboard(other_goal_id)
    assert [r.text for r in outsider.tap(f"{lessons.LISTEN}{lesson.id}")] == [t("button.expired")]
    assert [r.text for r in member.tap(f"{lessons.LISTEN}not-a-uuid")] == [t("button.expired")]
    assert [r.text for r in member.tap(f"{lessons.LISTEN}{uuid4()}")] == [t("button.expired")]
    assert [r.text for r in User(conversation).tap(f"{lessons.LISTEN}{lesson.id}")] == [t("onboarding.start_first")]
    assert synth.calls == []


def test_voice_replies_are_never_queued(sessions, clock, goal_id, make_voice_conversation):
    from app.core import outbox

    user = User(make_voice_conversation())
    user.onboard(goal_id)
    with sessions.begin() as session, pytest.raises(ValueError, match="not queued"):
        outbox.enqueue(session, session.merge(learner_of(sessions, user)), [Reply("", voice=b"OggS")],
                       key="voice", now=clock.now(session))


def test_inbound_repr_hides_voice_details():
    note = Note()
    text = repr(Inbound(Channel.TELEGRAM, "7012345678", "7012345678", voice=note.note))
    assert "7012345678" not in text and "voice=True" in text and note.fetches == 0


# Telegram


def voice_update(update_id: int, *, duration: int = 4, size: int = 9000) -> dict:
    update = message_update(update_id, None)
    update["message"]["voice"] = {"file_id": "voice-file", "file_unique_id": "voice-unique", "duration": duration,
                                  "mime_type": "audio/ogg", "file_size": size}
    return update


class VoiceBotAPI(FakeBotAPI):
    async def respond(self, method: str, parameters: dict) -> object:
        if method == "getFile":
            self.calls.append((method, parameters))
            return {"file_id": parameters["file_id"], "file_unique_id": "voice-unique", "file_size": len(AUDIO),
                    "file_path": "voice/file_1.oga"}
        if method == "sendVoice":
            self.calls.append((method, parameters))
            return {"message_id": 99, "date": 0, "chat": {"id": int(parameters["chat_id"]), "type": "private"},
                    "voice": {"file_id": "sent", "file_unique_id": "sent", "duration": 1}}
        return await super().respond(method, parameters)


class VoiceRequest(FakeRequest):
    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        if "/file/bot" in url:  # the file download
            self.api.calls.append(("download", {"url_ends": url.rsplit("/", 2)[-2:]}))
            return 200, AUDIO
        return await super().do_request(url, method, request_data, *args, **kwargs)


def test_telegram_voice_notes_download_only_when_fetched():
    api = VoiceBotAPI()

    async def scenario():
        bot = Bot(TOKEN, request=VoiceRequest(api))
        await bot.initialize()
        inbound = inbound_from_update(Update.de_json(voice_update(1, duration=4, size=9000), bot))
        assert inbound.text is None and inbound.payload is None
        assert (inbound.voice.duration_seconds, inbound.voice.size_bytes) == (4, 9000)
        assert "getFile" not in [method for method, _ in api.calls]
        audio = await asyncio.to_thread(inbound.voice.fetch)  # as the conversation does, from its worker thread
        await TelegramAdapter(bot).reply(inbound.chat_id, Reply("note", voice=b"OggS audio"))
        await bot.shutdown()
        return audio

    assert asyncio.run(scenario()) == AUDIO
    methods = [method for method, _ in api.calls]
    assert methods.index("getFile") < methods.index("download") < methods.index("sendMessage") < methods.index(
        "sendVoice")
    assert dict(api.calls)["download"]["url_ends"] == ["voice", "file_1.oga"]


def run_voice_session(app, api, sessions, clock, goal_id) -> list[dict]:
    """Onboard over Telegram, ask by voice, then tap Listen; returns the text messages sent."""
    with TestClient(app) as client:
        expected = 0
        for update, replies in [(message_update(1, "/start"), 2), (callback_update(2, onboarding.AGREE), 1),
                                (callback_update(3, "ob:lang:en"), 1), (message_update(4, "Amaka"), 1),
                                (callback_update(5, f"{onboarding.CHOOSE_GOAL}{goal_id}"), 3),
                                (voice_update(6), 2)]:
            api.push(update)
            expected += replies
            sent = api.wait_for_messages(expected)
        user = User(client.app.state.conversation)
        user.user_id = user.chat_id = "7012345678"
        lesson = add_lesson(sessions, clock, user, {"en": ENGLISH})
        api.push(callback_update(7, f"{lessons.LISTEN}{lesson.id}"))
        for _ in range(300):
            if "sendVoice" in [method for method, _ in api.calls]:
                break
            time.sleep(0.05)
    return sent


def test_telegram_end_to_end_voice_question_and_listen(database_url, engine, sessions, clock, make_client):
    with sessions.begin() as session:
        goal, _ = add_goal_with_chunks(session, [LEAF])
        goal_id = goal.id
    llm = make_client(FakeLLM(chat(answer_json("C1")), chat("yes")))
    transcriber = FakeTranscriber(LEAF_QUESTION)
    api = VoiceBotAPI()

    def fakes(application):  # before Telegram starts: scripted LLM, fake STT and TTS engines
        state = application.state
        speaker = Speaker(FakeSynthesizer(), FakeEncoder(), state.sessions, state.clock)
        state.conversation.qa = QAService(FakeEmbedder(), llm, state.clock, min_score=0.3)
        state.conversation.voice = VoiceService(state.sessions, transcriber=transcriber, speaker=speaker)
        state.conversation.lessons.audio = True
        return _null_context()

    settings = Settings(_env_file=None, database_url=database_url, hash_salt=SALT, telegram_enabled=True,
                        telegram_bot_token=TOKEN)
    app = create_app(settings, services=[fakes], telegram=lambda application: telegram_polling(
        application, request=VoiceRequest(api), get_updates_request=VoiceRequest(api)))
    try:
        sent = run_voice_session(app, api, sessions, clock, goal_id)
    finally:  # the Bot API fake has one synthetic user; leave no learner behind for test_telegram.py
        with sessions.begin() as session:
            learner = session.scalar(select(Learner).where(
                Learner.channel_user_hash == channel_hash(Channel.TELEGRAM, "7012345678", SALT)))
            if learner is not None:
                delete_learner(session, learner.id, confirmed=True)
    assert sent[-2]["text"] == t("voice.heard", text=LEAF_QUESTION)
    assert "Source: Synthetic Book" in sent[-1]["text"]
    assert transcriber.calls == [(AUDIO, Language.EN)]
    methods = [method for method, _ in api.calls]
    assert methods.count("getFile") == 1 and "sendVoice" in methods
    voice_calls = [parameters for method, parameters in api.calls if method == "sendVoice"]
    assert str(voice_calls[0]["chat_id"]) == "7012345678"


# Real engines (opt in)


speech = pytest.mark.skipif(os.environ.get("AJO_SPEECH_TESTS") != "1",
                            reason="set AJO_SPEECH_TESTS=1 to download and run the MMS-TTS and Whisper models")


@pytest.mark.speech
@speech
def test_real_english_audio_round_trips_through_whisper(sessions, clock):
    """MMS-TTS speaks an English question, ffmpeg encodes it, and faster-whisper hears it again."""
    ffmpeg = real_ffmpeg()
    if ffmpeg is None:
        pytest.skip("no ffmpeg on PATH or FFMPEG_PATH")
    speaker = Speaker(MMSSynthesizer({Language.EN: "facebook/mms-tts-eng", Language.YO: "facebook/mms-tts-yor"}),
                      OpusEncoder(ffmpeg), sessions, clock)
    # MMS's English voice is clearest on full sentences: Whisper hears its bare "What is photosynthesis?" as
    # "What is thought to say this is?". A human voice note is the real check (plan.md, milestone 8).
    question = "Please explain how photosynthesis works in green plants."
    audio = speaker.speak(question, Language.EN)
    assert audio.startswith(b"OggS") and speaker.speak(question, Language.EN) == audio
    heard = LocalWhisper(os.environ.get("STT_LOCAL_MODEL", "small")).transcribe(audio, Language.EN)
    assert "photosynthesis" in heard.lower()
    assert speaker.speak("Ẹ̀kọ́ òní jẹ́ nípa photosynthesis.", Language.YO).startswith(b"OggS")
