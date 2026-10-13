from dataclasses import dataclass, field

from app.adapters.base import Button


@dataclass(frozen=True)
class Reply:
    """One outbound message: text plus optional buttons. Adapters decide how to render it."""
    text: str
    buttons: list[Button] = field(default_factory=list)
