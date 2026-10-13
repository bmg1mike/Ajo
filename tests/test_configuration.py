import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def settings(**kwargs):
    return Settings(_env_file=None, hash_salt="synthetic-test-salt-with-32-characters", **kwargs)


def test_configuration_reads_environment(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("HASH_SALT=synthetic-test-salt-with-32-characters\nADMIN_IDS=123,456\nLLM_CACHE=false\n")
    monkeypatch.setenv("RETRIEVAL_MIN_SCORE", "0.9")
    configured = Settings(_env_file=env)
    assert configured.admin_user_ids == {123, 456}
    assert configured.retrieval_min_score == 0.9
    assert configured.llm_cache is False
    assert configured.hash_salt.get_secret_value() not in repr(configured)


def test_disabled_integrations_need_no_credentials():
    assert settings().telegram_enabled is False


@pytest.mark.parametrize("kwargs", [
    {"hash_salt": ""},
    {"hash_salt": "too-short"},
    {"telegram_enabled": True},
    {"llm_enabled": True},
    {"llm_enabled": True, "llm_model": "demo", "llm_api_key": "test", "llm_base_url": "invalid"},
    {"stt_enabled": True, "stt_backend": "groq"},
    {"stt_backend": "unsupported"},
    {"retrieval_min_score": 1.1},
    {"admin_ids": "123,bad"},
])
def test_invalid_configuration_fails(kwargs):
    values = {"hash_salt": "synthetic-test-salt-with-32-characters", **kwargs}
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_enabled_integrations_accept_complete_settings():
    configured = settings(telegram_enabled=True, telegram_bot_token="synthetic-token",
                          llm_enabled=True, llm_base_url="http://localhost:8080/v1",
                          llm_api_key="synthetic-key", llm_model="demo",
                          stt_enabled=True, stt_backend="groq", groq_api_key="synthetic-key")
    assert configured.llm_enabled
