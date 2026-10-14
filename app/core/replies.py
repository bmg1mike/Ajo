from dataclasses import dataclass, field

from app.adapters.base import Button


@dataclass(frozen=True)
class Attachment:
    """A file sent after its message's text, such as the weekly study pack."""
    filename: str
    data: bytes


@dataclass(frozen=True)
class Reply:
    """One outbound message: text plus optional buttons and file. Adapters decide how to render it."""
    text: str
    buttons: list[Button] = field(default_factory=list)
    file: Attachment | None = None
