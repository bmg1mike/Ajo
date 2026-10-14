"""OpenAI-compatible chat completions client with retries, backoff and a prompt-hash cache.

Synchronous like the rest of the core (sync SQLAlchemy sessions, CPU-bound embeddings);
async adapters should call it through `asyncio.to_thread`.
"""
import hashlib
import json
import random
import re
import time
from collections.abc import Callable, Sequence
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.models import LLMCache

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.clock import Clock

Message = dict[str, str]

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_REASONING = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(Exception):
    """The provider failed or returned an unusable response."""


class LLMUnavailable(LLMError):
    """No live LLM is configured and the prompt is not cached."""


class LLMClient:
    def __init__(self, *, sessions: sessionmaker[Session], clock: "Clock", base_url: str, api_key: str,
                 model: str, enabled: bool, cache: bool, temperature: float | None = None,
                 reasoning_effort: str = "", timeout: float = 60.0, max_attempts: int = 4,
                 backoff_seconds: float = 1.0, max_backoff_seconds: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep, transport: httpx.BaseTransport | None = None) -> None:
        self._sessions, self._clock = sessions, clock
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model, self._enabled, self._cache = model, enabled, cache
        self._temperature, self._reasoning_effort = temperature, reasoning_effort.strip()
        self._max_attempts, self._backoff, self._max_backoff = max_attempts, backoff_seconds, max_backoff_seconds
        self._sleep = sleep
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"Authorization": f"Bearer {api_key}"} if api_key else {})

    @classmethod
    def from_settings(cls, settings: "Settings", sessions: sessionmaker[Session], clock: "Clock",
                      **overrides) -> "LLMClient":
        options = dict(base_url=settings.llm_base_url, api_key=settings.llm_api_key.get_secret_value(),
                       model=settings.llm_model, enabled=settings.llm_enabled, cache=settings.llm_cache,
                       temperature=settings.llm_temperature, reasoning_effort=settings.llm_reasoning_effort,
                       timeout=settings.llm_timeout_seconds)
        return cls(sessions=sessions, clock=clock, **(options | overrides))

    def close(self) -> None:
        self._http.close()

    def complete(self, messages: Sequence[Message], *, json_output: bool = False) -> str:
        """Return the assistant message text, from the cache when possible."""
        body: dict = {"model": self._model, "messages": list(messages)}
        if json_output:
            body["response_format"] = {"type": "json_object"}
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if self._reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        key = self.cache_key(body)
        if self._cache and (cached := self._cache_get(key)) is not None:
            return cached
        if not self._enabled:
            raise LLMUnavailable("LLM is disabled and the prompt is not cached")
        content = self._request(body)
        if self._cache:
            self._cache_put(key, content)
        return content

    def cache_key(self, body: dict) -> str:
        # Provider, model and every request parameter, so a config change never reuses a stale answer.
        material = json.dumps({"url": self._url, **body}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _request(self, body: dict) -> str:
        for attempt in range(1, self._max_attempts + 1):
            retry_after = None
            try:
                response = self._http.post(self._url, json=body)
            except httpx.TransportError as exc:
                problem = f"transport error: {type(exc).__name__}"
            else:
                if response.status_code == 200:
                    return self._content(response)
                if response.status_code not in RETRYABLE_STATUS:
                    raise LLMError(f"LLM request failed with HTTP {response.status_code}")
                problem = f"HTTP {response.status_code}"
                retry_after = retry_after_seconds(response)
            if attempt == self._max_attempts:
                raise LLMError(f"LLM request failed after {attempt} attempts ({problem})")
            delay = self._backoff * 2 ** (attempt - 1) * (1 + random.random() / 2)
            self._sleep(min(self._max_backoff, max(delay, retry_after or 0.0)))
        raise AssertionError("unreachable")

    @staticmethod
    def _content(response: httpx.Response) -> str:
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("LLM response did not match the chat completions format") from None
        if content is not None and not isinstance(content, str):
            raise LLMError(f"LLM message content is {type(content).__name__}, not text")
        content = _REASONING.sub("", content or "").strip()
        if not content:
            raise LLMError("LLM returned an empty message")
        return content

    def _cache_get(self, key: str) -> str | None:
        with self._sessions() as session:
            row = session.get(LLMCache, key)
            content = row.response.get("content") if row and isinstance(row.response, dict) else None
            return content if isinstance(content, str) else None  # anything else is a miss

    def _cache_put(self, key: str, content: str) -> None:
        # Own transaction: a cached response survives the caller rolling back its work.
        with self._sessions.begin() as session:
            insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
            session.execute(insert(LLMCache).values(prompt_hash=key, response={"content": content},
                                                    created_at=self._clock.now(session))
                            .on_conflict_do_nothing(index_elements=["prompt_hash"]))


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None
