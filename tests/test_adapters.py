import asyncio
from unittest.mock import AsyncMock

import pytest

from app.adapters.base import AdapterRegistry, Button
from app.models import Channel, Learner


def test_registry_dispatch_and_simulated_delivery_suppression():
    adapter = AsyncMock()
    adapter.channel = Channel.WEB
    registry = AdapterRegistry()
    registry.register(adapter)
    learner = Learner(channel=Channel.WEB, is_simulated=False)
    buttons = [Button("Demo", "demo")]
    asyncio.run(registry.send_text(learner, "Hello", buttons))
    asyncio.run(registry.send_voice(learner, b"voice"))
    asyncio.run(registry.send_file(learner, "demo.txt", b"demo"))
    adapter.send_text.assert_awaited_once_with(learner, "Hello", buttons)
    adapter.send_voice.assert_awaited_once_with(learner, b"voice")
    adapter.send_file.assert_awaited_once_with(learner, "demo.txt", b"demo")
    learner.is_simulated = True
    asyncio.run(registry.send_text(learner, "Ignored"))
    asyncio.run(registry.send_voice(learner, b"ignored"))
    asyncio.run(registry.send_file(learner, "ignored", b"ignored"))
    assert adapter.send_text.await_count == 1
    assert adapter.send_voice.await_count == 1
    assert adapter.send_file.await_count == 1


def test_registry_rejects_duplicate_and_missing_adapters():
    registry = AdapterRegistry()
    with pytest.raises(LookupError):
        registry.get(Channel.USSD)
    adapter = AsyncMock()
    adapter.channel = Channel.WEB
    registry.register(adapter)
    with pytest.raises(ValueError):
        registry.register(adapter)
