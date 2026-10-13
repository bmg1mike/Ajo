"""Download source PDFs and extract configured sections with printed page numbers."""
import hashlib
import re
import unicodedata
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import Destination

from ingest.catalog import SourceSpec

PAGE_INFERENCE_WINDOW = 3


class IngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Segment:
    page: int  # printed page number, as cited to learners
    text: str


@dataclass(frozen=True)
class SectionText:
    title: str
    segments: tuple[Segment, ...]


@dataclass
class ExtractionStats:
    pdf_pages: set[int] = field(default_factory=set)
    page_label_fallbacks: set[int] = field(default_factory=set)
    heading_misses: list[str] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_pdf(spec: SourceSpec, corpus_dir: Path, *, offline: bool) -> Path:
    path = corpus_dir / spec.file
    if not path.exists():
        if offline:
            raise IngestError(f"{spec.key}: {path} is missing and downloads are disabled")
        _download(str(spec.download_url), path, spec.sha256)
    actual = sha256_file(path)
    if actual != spec.sha256:
        # Publishers revise PDFs in place; page and section mappings must be re-checked.
        raise IngestError(f"{spec.key}: {path} has SHA-256 {actual}, expected {spec.sha256}. "
                          "Delete the file to re-download, or review the new edition and update sources.yaml.")
    return path


def _download(url: str, path: Path, expected_sha256: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "ajo-ingest/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as handle:
            for block in iter(lambda: response.read(1 << 20), b""):
                handle.write(block)
        if sha256_file(partial) != expected_sha256:
            raise IngestError(f"Downloaded {url} does not match the SHA-256 recorded in sources.yaml")
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def normalize_heading(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("’", "'").replace("‘", "'")
    return " ".join(text.split()).casefold()


def _flatten_outline(reader: PdfReader) -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []

    def walk(items) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item)
            elif isinstance(item, Destination):
                page = reader.get_destination_page_number(item)
                if page is not None and page >= 0:
                    entries.append((item.title or "", page))

    walk(reader.outline)
    return entries


def _join_lines(lines: Sequence[str]) -> str:
    parts: list[str] = []
    for line in lines:
        if parts and parts[-1].endswith("-"):
            parts[-1] += line  # rejoin words hyphenated at a line break, keeping the hyphen
        else:
            parts.append(line)
    return " ".join(" ".join(parts).split())


def _find_heading(lines: Sequence[str], title: str, *, start: int = 0, last: bool = False) -> int | None:
    """Index of the line where `title` begins; headings may wrap onto a second line."""
    target = normalize_heading(title)
    found = None
    for index in range(start, len(lines)):
        line = normalize_heading(lines[index])
        joined = normalize_heading(lines[index] + " " + lines[index + 1]) if index + 1 < len(lines) else line
        if line == target or joined == target:
            if not last:
                return index
            found = index
    return found


class SourceDocument:
    def __init__(self, spec: SourceSpec, path: Path) -> None:
        self.spec = spec
        self.reader = PdfReader(path)
        self.outline = _flatten_outline(self.reader)
        self.patterns = [re.compile(pattern) for pattern in spec.line_patterns]
        self.stats = ExtractionStats()
        self._pages: dict[int, tuple[int | None, list[str]]] = {}

    def page(self, index: int) -> tuple[int, list[str]]:
        """Return (printed page number, content lines) with header/footer lines removed."""
        footer_page, lines = self._read(index)
        if footer_page is not None:
            return footer_page, lines
        # Chapter openers carry no footer; infer from the nearest page that has one.
        for distance in range(1, PAGE_INFERENCE_WINDOW + 1):
            for neighbour in (index - distance, index + distance):
                if 0 <= neighbour < len(self.reader.pages):
                    neighbour_page = self._read(neighbour)[0]
                    if neighbour_page is not None:
                        return neighbour_page + index - neighbour, lines
        self.stats.page_label_fallbacks.add(index)
        label = self.reader.page_labels[index]
        return (int(label) if label.isdigit() else index + 1), lines

    def _read(self, index: int) -> tuple[int | None, list[str]]:
        if index not in self._pages:
            printed = None
            lines = []
            for raw in (self.reader.pages[index].extract_text() or "").splitlines():
                line = raw.strip()
                if not line:
                    continue
                match = next((m for pattern in self.patterns if (m := pattern.search(line))), None)
                if match is None:
                    lines.append(line)
                elif "page" in match.re.groupindex and match.group("page"):
                    printed = int(match.group("page"))
            self._pages[index] = (printed, lines)
        return self._pages[index]

    def section(self, title: str) -> SectionText:
        target = normalize_heading(title)
        positions = [i for i, (entry, _) in enumerate(self.outline) if normalize_heading(entry) == target]
        if len(positions) != 1:
            problem = "not found in" if not positions else "ambiguous in"
            raise IngestError(f"{self.spec.key}: section {title!r} is {problem} the PDF outline")
        position = positions[0]
        first = self.outline[position][1]
        following = self.outline[position + 1] if position + 1 < len(self.outline) else None
        last = following[1] if following else len(self.reader.pages) - 1
        if last < first:
            raise IngestError(f"{self.spec.key}: outline entry after {title!r} points backwards")

        segments: list[Segment] = []
        for index in range(first, last + 1):
            printed, lines = self.page(index)
            begin, end = 0, len(lines)
            if index == first:
                # Use the last match: chapter-opening pages also list section titles in an outline box.
                heading = _find_heading(lines, title, last=True)
                if heading is None:
                    self.stats.heading_misses.append(f"{self.spec.key}: start of {title!r} (pdf page {index + 1})")
                else:
                    begin = heading + _heading_lines(lines, heading, title)
            if following is not None and index == last:
                boundary = _find_heading(lines, following[0], start=begin)
                if boundary is not None:
                    end = boundary
                else:
                    self.stats.heading_misses.append(
                        f"{self.spec.key}: end of {title!r} at {following[0]!r} (pdf page {index + 1})")
                    if index != first:
                        # Without a detectable boundary the page belongs to the next entry; skip it.
                        continue
            text = _join_lines(lines[begin:end])
            if text:
                self.stats.pdf_pages.add(index)
                segments.append(Segment(page=printed, text=text))
        if not segments:
            raise IngestError(f"{self.spec.key}: section {title!r} produced no text")
        return SectionText(title=" ".join(title.split()), segments=tuple(segments))


def _heading_lines(lines: Sequence[str], index: int, title: str) -> int:
    return 1 if normalize_heading(lines[index]) == normalize_heading(title) else 2
