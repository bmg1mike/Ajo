"""Split section text into token-bounded, overlapping chunks that never cross sections."""
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ingest.pdf import Segment

TokenCounter = Callable[[str], int]

# Split after sentence punctuation when the next sentence starts like a sentence.
_NEXT_SENTENCE = r"\s+(?=[\"“(‘]?[A-Z0-9•])"
SENTENCE_BREAK = re.compile(rf"(?<=[.!?]){_NEXT_SENTENCE}|(?<=[.!?][\"”’)]){_NEXT_SENTENCE}")


@dataclass(frozen=True)
class ChunkDraft:
    section: str
    page: int  # printed page where the chunk starts
    text: str
    token_count: int


@dataclass(frozen=True)
class _Unit:
    page: int
    text: str


@dataclass
class _Chunk:
    overlap: list[_Unit] = field(default_factory=list)  # carried from the previous chunk
    body: list[_Unit] = field(default_factory=list)

    @property
    def units(self) -> list[_Unit]:
        return self.overlap + self.body


def chunk_section(section: str, segments: Sequence[Segment], count_tokens: TokenCounter, *,
                  min_tokens: int = 300, max_tokens: int = 500, overlap_tokens: int = 50) -> list[ChunkDraft]:
    """Greedily pack sentences into chunks of at most `max_tokens` tokens.

    Each chunk after the first starts with the longest run of whole trailing words of
    its predecessor that fits in `overlap_tokens`. Every chunk except a section's last
    reaches `min_tokens`: when the next sentence would overflow a chunk that is still
    short, the sentence is split at a word boundary to fill the chunk. The last chunk
    is topped up from its predecessor where possible.
    """
    if not 0 <= overlap_tokens < min_tokens <= max_tokens:
        raise ValueError("Require 0 <= overlap_tokens < min_tokens <= max_tokens")
    def tokens(units: Sequence[_Unit]) -> int:
        return count_tokens(_join(units))

    units = [unit for segment in segments for unit in _units(segment, count_tokens, max_tokens - overlap_tokens)]
    chunks: list[_Chunk] = []
    current = _Chunk()
    for unit in units:
        if current.body and tokens(current.units + [unit]) > max_tokens:
            if tokens(current.units) < min_tokens:
                head, unit = _fill(current.units, unit, count_tokens, max_tokens)
                if head is not None:
                    current.body.append(head)
            chunks.append(current)
            current = _Chunk(overlap=_overlap(current.units, count_tokens, overlap_tokens))
            if tokens(current.overlap + [unit]) > max_tokens:
                current.overlap = []  # token counts are not strictly additive across joins
        current.body.append(unit)
    if current.body:
        chunks.append(current)
    _rebalance_tail(chunks, count_tokens, min_tokens, max_tokens, overlap_tokens)
    drafts = []
    for chunk in chunks:
        text = _join(chunk.units)
        drafts.append(ChunkDraft(section=section, page=chunk.units[0].page, text=text, token_count=count_tokens(text)))
    return drafts


def _join(units: Sequence[_Unit]) -> str:
    return " ".join(unit.text for unit in units)


def _units(segment: Segment, count_tokens: TokenCounter, limit: int) -> list[_Unit]:
    units = []
    for sentence in SENTENCE_BREAK.split(segment.text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if count_tokens(sentence) <= limit:
            units.append(_Unit(segment.page, sentence))
        else:
            units.extend(_Unit(segment.page, part) for part in _split_words(sentence, count_tokens, limit))
    return units


def _split_words(text: str, count_tokens: TokenCounter, limit: int) -> list[str]:
    """Split an over-long sentence into consecutive word windows within `limit` tokens."""
    words = text.split()
    parts = []
    while words:
        size = _largest_fit(len(words), lambda n, words=words: count_tokens(" ".join(words[:n])) <= limit)
        if size == 0:
            raise ValueError("A single word exceeds the chunk token limit")
        parts.append(" ".join(words[:size]))
        words = words[size:]
    return parts


def _fill(units: Sequence[_Unit], unit: _Unit, count_tokens: TokenCounter,
          max_tokens: int) -> tuple[_Unit | None, _Unit]:
    """Split `unit` into the leading words that still fit after `units`, and the rest."""
    words = unit.text.split()
    size = _largest_fit(len(words) - 1, lambda n: count_tokens(_join([*units, _Unit(unit.page, " ".join(words[:n]))]))
                        <= max_tokens)
    if size == 0:
        return None, unit
    return _Unit(unit.page, " ".join(words[:size])), _Unit(unit.page, " ".join(words[size:]))


def _overlap(units: Sequence[_Unit], count_tokens: TokenCounter, overlap_tokens: int) -> list[_Unit]:
    """Trailing whole words of `units` within `overlap_tokens`, attributed to their first word's page."""
    tagged = [(unit.page, word) for unit in units for word in unit.text.split()]
    size = _largest_fit(len(tagged), lambda n: count_tokens(" ".join(word for _, word in tagged[-n:])) <= overlap_tokens)
    if size == 0:
        return []
    tail = tagged[-size:]
    return [_Unit(tail[0][0], " ".join(word for _, word in tail))]


def _largest_fit(upper: int, fits: Callable[[int], bool]) -> int:
    """Largest n in [0, upper] with fits(n); fits must be monotone (true, then false)."""
    low, high = 0, upper
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


def _rebalance_tail(chunks: list[_Chunk], count_tokens: TokenCounter, min_tokens: int,
                    max_tokens: int, overlap_tokens: int) -> None:
    """Move trailing sentences of the penultimate chunk into a short final chunk."""
    def tokens(units: Sequence[_Unit]) -> int:
        return count_tokens(_join(units))

    if len(chunks) < 2:
        return
    previous, last = chunks[-2], chunks[-1]
    while tokens(last.units) < min_tokens and len(previous.body) > 1:
        shorter = _Chunk(previous.overlap, previous.body[:-1])
        longer = _Chunk(_overlap(shorter.units, count_tokens, overlap_tokens), [previous.body[-1], *last.body])
        if tokens(shorter.units) < min_tokens or tokens(longer.units) > max_tokens:
            break
        previous, last = shorter, longer
    chunks[-2], chunks[-1] = previous, last
