from datetime import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_WEB_TOKEN_CHARS = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ajo:ajo@localhost:5432/ajo"
    hash_salt: SecretStr
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr = SecretStr("")
    # A comma-separated string keeps .env configuration simple.
    admin_ids: str = ""
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    # Optional request parameters; leave unset for models that reject them
    # (OpenAI reasoning models accept only the default temperature).
    llm_temperature: float | None = None
    llm_reasoning_effort: str = ""
    llm_timeout_seconds: float = 60.0
    stt_enabled: bool = False
    stt_backend: Literal["local", "groq"] = "local"
    groq_api_key: SecretStr = SecretStr("")
    # Voice notes (milestone 8): a faster-whisper model size or path, or a Groq Whisper model.
    stt_local_model: str = "small"
    stt_groq_model: str = "whisper-large-v3-turbo"
    stt_max_seconds: int = 60  # longer voice notes are refused before they are downloaded
    # Lesson audio: an MMS-TTS model per language (blank: none), encoded to OGG/Opus with ffmpeg.
    # English is required, because it is the fallback for languages without a reviewed voice.
    tts_enabled: bool = False
    tts_model_en: str = "facebook/mms-tts-eng"
    tts_model_yo: str = "facebook/mms-tts-yor"
    tts_model_pcm: str = "facebook/mms-tts-pcm"
    ffmpeg_path: str = "ffmpeg"
    embed_model: str = "intfloat/multilingual-e5-small"
    retrieval_min_score: float = 0.80
    llm_cache: bool = True
    # The scheduler runs each circle's daily jobs when the virtual clock passes LESSON_TIME (UTC).
    # Off by default so tests and scripted demos are driven by /simulate_day alone.
    scheduler_enabled: bool = False
    lesson_time: time = time(7, 0)  # 08:00 in Lagos
    demo_goal: str = "jamb_bio"  # the goal /seed_demo uses when the admin has not onboarded
    # Hand-checked opportunities for weekly payouts, synced into the database at startup.
    opportunities_file: Path = Path("data/opportunities.json")
    # The web simulator (GET /sim) can speak for any web learner and run the admin commands, so it
    # is off by default and every /web request must carry WEB_TOKEN as a bearer token.
    web_enabled: bool = False
    web_token: SecretStr = SecretStr("")

    @field_validator("hash_salt")
    @classmethod
    def validate_salt(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 32:
            raise ValueError("HASH_SALT must contain at least 32 characters")
        return value

    @field_validator("retrieval_min_score")
    @classmethod
    def validate_score(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("RETRIEVAL_MIN_SCORE must be between 0 and 1")
        return value

    @field_validator("stt_max_seconds")
    @classmethod
    def validate_voice_length(cls, value: int) -> int:
        if not 1 <= value <= 600:
            raise ValueError("STT_MAX_SECONDS must be between 1 and 600")
        return value

    @field_validator("llm_temperature", mode="before")
    @classmethod
    def blank_temperature(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("admin_ids")
    @classmethod
    def validate_admins(cls, value: str) -> str:
        if any(not item.strip().isdigit() for item in value.split(",") if item.strip()):
            raise ValueError("ADMIN_IDS must be comma-separated numeric IDs")
        return value

    @property
    def admin_user_ids(self) -> frozenset[int]:
        return frozenset(int(item.strip()) for item in self.admin_ids.split(",") if item.strip())

    @model_validator(mode="after")
    def validate_integrations(self) -> "Settings":
        if self.telegram_enabled and not self.telegram_bot_token.get_secret_value().strip():
            raise ValueError("TELEGRAM_BOT_TOKEN is required when Telegram is enabled")
        if self.llm_enabled:
            if not self.llm_model.strip() or not self.llm_api_key.get_secret_value().strip():
                raise ValueError("LLM_MODEL and LLM_API_KEY are required when LLM is enabled")
            url = urlparse(self.llm_base_url)
            if url.scheme not in {"http", "https"} or not url.netloc:
                raise ValueError("LLM_BASE_URL must be an HTTP(S) URL when LLM is enabled")
        if self.stt_enabled and self.stt_backend == "groq" and not self.groq_api_key.get_secret_value().strip():
            raise ValueError("GROQ_API_KEY is required when Groq STT is enabled")
        if self.stt_enabled and self.stt_backend == "local" and not self.stt_local_model.strip():
            raise ValueError("STT_LOCAL_MODEL is required when local STT is enabled")
        if self.web_enabled and len(self.web_token.get_secret_value().strip()) < MIN_WEB_TOKEN_CHARS:
            raise ValueError(f"WEB_TOKEN of at least {MIN_WEB_TOKEN_CHARS} characters is required when the web "
                             "simulator is enabled")
        if self.tts_enabled and not self.tts_model_en.strip():
            raise ValueError("TTS_MODEL_EN is required when TTS is enabled: English is the audio fallback")
        return self

    @property
    def tts_models(self) -> dict[str, str]:
        """Language code to MMS-TTS model, for the languages that have one configured."""
        models = {"en": self.tts_model_en, "yo": self.tts_model_yo, "pcm": self.tts_model_pcm}
        return {language: model.strip() for language, model in models.items() if model.strip()}
