"""Typed loaders for the source catalog (sources.yaml) and the syllabus (JSON)."""
import json
import re
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SectionSpec(Strict):
    # Must equal a PDF outline (bookmark) title, e.g. "5.1 Overview of Photosynthesis".
    title: str = Field(min_length=1)
    # Optional manual override from spot checks; otherwise topics come from embedding similarity.
    topic: str | None = Field(default=None, pattern=KEY_PATTERN)


class SourceSpec(Strict):
    key: str = Field(pattern=KEY_PATTERN)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    license: str = Field(min_length=1)
    license_url: HttpUrl
    url: HttpUrl
    download_url: HttpUrl
    file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.pdf$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Lines matching any pattern are removed (running headers/footers). A named
    # group `page` supplies the printed page number used in citations.
    line_patterns: tuple[str, ...] = ()
    sections: tuple[SectionSpec, ...] = Field(min_length=1)

    @field_validator("download_url")
    @classmethod
    def require_https(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("download_url must use HTTPS")
        return value

    @field_validator("line_patterns")
    @classmethod
    def compile_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"Invalid line pattern {pattern!r}: {exc}") from None
        return value

    @field_validator("sections", mode="before")
    @classmethod
    def accept_plain_titles(cls, value):
        if isinstance(value, list):
            return [{"title": item} if isinstance(item, str) else item for item in value]
        return value

    @model_validator(mode="after")
    def unique_sections(self) -> "SourceSpec":
        titles = [section.title for section in self.sections]
        if len(titles) != len(set(titles)):
            raise ValueError(f"Source {self.key} lists a section more than once")
        return self


class Catalog(Strict):
    sources: tuple[SourceSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_keys(self) -> "Catalog":
        for field in ("key", "file"):
            values = [getattr(source, field) for source in self.sources]
            if len(values) != len(set(values)):
                raise ValueError(f"Source {field}s must be unique")
        return self


class GoalSpec(Strict):
    code: str = Field(pattern=KEY_PATTERN)
    title: str = Field(min_length=1)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    exam_date: date | None = None


class TopicSpec(Strict):
    code: str = Field(pattern=KEY_PATTERN)
    title: str = Field(min_length=1)
    objectives: str = Field(min_length=1)


class Syllabus(Strict):
    goal: GoalSpec
    # List order is the teaching order; order_index is the 1-based position.
    topics: tuple[TopicSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_codes(self) -> "Syllabus":
        codes = [topic.code for topic in self.topics]
        if len(codes) != len(set(codes)):
            raise ValueError("Topic codes must be unique")
        return self


def load_catalog(path: Path) -> Catalog:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # Top-level `x-` keys hold YAML anchors shared between sources, as in Compose files.
        data = {key: value for key, value in data.items() if not str(key).startswith("x-")}
    return Catalog.model_validate(data)


def load_syllabus(path: Path) -> Syllabus:
    return Syllabus.model_validate(json.loads(path.read_text(encoding="utf-8")))


def check_topic_overrides(catalog: Catalog, syllabus: Syllabus) -> None:
    codes = {topic.code for topic in syllabus.topics}
    for source in catalog.sources:
        for section in source.sections:
            if section.topic is not None and section.topic not in codes:
                raise ValueError(f"{source.key}: section {section.title!r} names unknown topic {section.topic!r}")
