from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    stt_enabled: bool = False
    stt_backend: Literal["local", "groq"] = "local"
    groq_api_key: SecretStr = SecretStr("")
    tts_model_yo: str = "facebook/mms-tts-yor"
    embed_model: str = "intfloat/multilingual-e5-small"
    retrieval_min_score: float = 0.80
    llm_cache: bool = True

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
        return self
