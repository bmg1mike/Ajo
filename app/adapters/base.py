from dataclasses import dataclass
from typing import Protocol

from app.models import Channel, Learner


@dataclass(frozen=True)
class Button:
    text: str
    payload: str


class Adapter(Protocol):
    channel: Channel

    async def send_text(self, learner: Learner, text: str, buttons: list[Button] | None = None) -> None: ...
    async def send_voice(self, learner: Learner, ogg_bytes: bytes) -> None: ...
    async def send_file(self, learner: Learner, filename: str, data: bytes) -> None: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[Channel, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        if adapter.channel in self._adapters:
            raise ValueError(f"Adapter already registered for {adapter.channel}")
        self._adapters[adapter.channel] = adapter

    def unregister(self, channel: Channel) -> None:
        self._adapters.pop(channel, None)

    @property
    def channels(self) -> frozenset[Channel]:
        return frozenset(self._adapters)

    def get(self, channel: Channel) -> Adapter:
        try:
            return self._adapters[channel]
        except KeyError:
            raise LookupError(f"No adapter registered for {channel}") from None

    async def send_text(self, learner: Learner, text: str, buttons: list[Button] | None = None) -> None:
        if not learner.is_simulated:
            await self.get(learner.channel).send_text(learner, text, buttons)

    async def send_voice(self, learner: Learner, ogg_bytes: bytes) -> None:
        if not learner.is_simulated:
            await self.get(learner.channel).send_voice(learner, ogg_bytes)

    async def send_file(self, learner: Learner, filename: str, data: bytes) -> None:
        if not learner.is_simulated:
            await self.get(learner.channel).send_file(learner, filename, data)
