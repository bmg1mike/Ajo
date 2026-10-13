import json
import random
import re
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import delete, func, select

from app.core import embeddings
from app.models import Chunk, Circle, Goal, Lesson, PregeneratedLesson, Question, Source, SyllabusTopic
from ingest import ingest
from ingest.catalog import load_catalog, load_syllabus
from ingest.chunking import chunk_section
from ingest.ingest import build_corpus, coverage_problems, format_report, persist
from ingest.pdf import IngestError, Segment, SourceDocument, ensure_pdf, sha256_file
from tests.fakes import FakeEmbedder
from tests.pdf_builder import build_pdf

SALT = "synthetic-test-salt-with-32-characters"
LEAF_WORDS = "chlorophyll light leaf sugar carbon dioxide stomata glucose"
KIDNEY_WORDS = "kidney nephron urine urea filtration bladder water salt"


def sentences(words: str, count: int, start: int = 0) -> list[str]:
    return [f"Fact {number} links {words}." for number in range(start, start + count)]


def write_inputs(tmp_path: Path, *, sections=None, key="synthetic-book", topics=None,
                 goal_code="test_bio") -> dict[str, Path]:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    pdf = build_pdf(corpus_dir / "book.pdf", [
        # Chapter opener: no footer, and an outline box repeating the section titles.
        ["Chapter 1 Living Processes", "Chapter Outline", "1.1 Leaves Make Food", "1.2 Kidneys Filter Blood",
         "1.1 Leaves Make Food", *sentences(LEAF_WORDS, 30)],
        [*sentences(LEAF_WORDS, 30, 30), "Page 11"],
        [*sentences(LEAF_WORDS, 10, 60), "1.2 Kidneys", "Filter Blood", *sentences(KIDNEY_WORDS, 30), "Page 12"],
        [*sentences(KIDNEY_WORDS, 40, 30), "Page 13"],
        ["Key Terms", "glossary entry that must not be ingested", "Page 14"],
    ], [("Chapter 1 Living Processes", 0), ("1.1 Leaves Make Food", 0), ("1.2 Kidneys Filter Blood", 2),
        ("Key Terms", 4)])
    catalog = {"x-patterns": ["unused"], "sources": [{
        "key": key, "title": "Synthetic Biology", "publisher": "Test Press", "license": "CC0 1.0",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/", "url": "https://example.org/book",
        "download_url": "https://example.org/book.pdf", "file": "book.pdf", "sha256": sha256_file(pdf),
        "line_patterns": [r"^Page (?P<page>\d+)$"],
        "sections": sections or ["1.1 Leaves Make Food", "1.2 Kidneys Filter Blood"],
    }]}
    syllabus = {"goal": {"code": goal_code, "title": "Test Biology", "country": "NG", "exam_date": None},
                "topics": topics or [
                    {"code": "photosynthesis", "title": "Photosynthesis", "objectives": LEAF_WORDS},
                    {"code": "excretion", "title": "Excretion", "objectives": KIDNEY_WORDS}]}
    paths = {"sources": tmp_path / "sources.yaml", "syllabus": tmp_path / "syllabus.json", "corpus": corpus_dir}
    paths["sources"].write_text(yaml.safe_dump(catalog, allow_unicode=True), encoding="utf-8")
    paths["syllabus"].write_text(json.dumps(syllabus), encoding="utf-8")
    return paths


def build(paths: dict[str, Path]):
    return build_corpus(load_catalog(paths["sources"]), load_syllabus(paths["syllabus"]), paths["corpus"],
                        FakeEmbedder(), offline=True)


@pytest.fixture
def clean_corpus(sessions):
    def wipe():
        with sessions.begin() as session:
            for model in (Question, Lesson, PregeneratedLesson, Circle, Chunk, Source, SyllabusTopic, Goal):
                session.execute(delete(model))
    wipe()
    yield
    wipe()


# Chunking

def words_tokens(text: str) -> int:
    return len(text.split())


def test_chunks_stay_within_bounds_and_overlap():
    segments = [Segment(page, " ".join(sentences(LEAF_WORDS, 40, page * 40))) for page in (1, 2, 3)]
    chunks = chunk_section("1.1 Leaves", segments, words_tokens, min_tokens=60, max_tokens=100, overlap_tokens=10)
    assert len(chunks) > 3
    assert all(chunk.token_count <= 100 for chunk in chunks)
    assert all(chunk.token_count >= 60 for chunk in chunks)
    assert all(chunk.section == "1.1 Leaves" for chunk in chunks)
    for previous, current in pairwise(chunks):
        overlap = " ".join(previous.text.split()[-10:])
        assert current.text.startswith(overlap)
    # Every source sentence survives chunking.
    joined = " ".join(chunk.text for chunk in chunks)
    assert all(sentence in joined for segment in segments for sentence in re.split(r"(?<=\.) ", segment.text))


def test_short_chunk_is_filled_before_an_overflowing_sentence():
    # Regression: a 30-token chunk followed by an 85-token sentence used to be emitted below the minimum.
    first = " ".join(f"A{n}" for n in range(29)) + "."
    long = " ".join(f"B{n}" for n in range(84)) + "."
    chunks = chunk_section("s", [Segment(1, f"{first} {long}")], words_tokens,
                           min_tokens=60, max_tokens=100, overlap_tokens=10)
    assert chunks[0].token_count == 100
    assert all(chunk.token_count >= 60 for chunk in chunks[:-1])
    assert " ".join(chunk.text for chunk in chunks).endswith("B83.")


@pytest.mark.parametrize("seed", range(25))
def test_non_final_chunks_meet_minimum_for_varied_sentence_lengths(seed):
    rng = random.Random(seed)
    text = " ".join(" ".join(f"W{n}" for n in range(rng.randint(1, 95))) + "." for _ in range(rng.randint(5, 40)))
    chunks = chunk_section("s", [Segment(1, text)], words_tokens, min_tokens=60, max_tokens=100, overlap_tokens=10)
    assert all(chunk.token_count <= 100 for chunk in chunks)
    assert all(chunk.token_count >= 60 for chunk in chunks[:-1])


def test_chunk_page_is_where_its_text_starts():
    segments = [Segment(7, " ".join(sentences(LEAF_WORDS, 12))), Segment(8, " ".join(sentences(LEAF_WORDS, 12, 12)))]
    chunks = chunk_section("s", segments, words_tokens, min_tokens=60, max_tokens=100, overlap_tokens=10)
    assert chunks[0].page == 7
    for chunk in chunks:
        first_fact = int(re.search(r"Fact (\d+)", chunk.text).group(1))
        assert chunk.page == (7 if first_fact < 12 else 8)


def test_short_final_chunk_is_topped_up_from_previous():
    # 11 sentences of 10 tokens: greedy packing would leave a 10 + 10 token tail.
    segments = [Segment(1, " ".join(f"Short sentence {n} has exactly ten words in it." for n in range(11)))]
    chunks = chunk_section("s", segments, words_tokens, min_tokens=40, max_tokens=100, overlap_tokens=10)
    assert [chunk.token_count >= 40 for chunk in chunks] == [True] * len(chunks)


def test_short_section_yields_one_short_chunk():
    chunks = chunk_section("s", [Segment(3, "Only one sentence.")], words_tokens)
    assert [(chunk.page, chunk.text, chunk.token_count) for chunk in chunks] == [(3, "Only one sentence.", 3)]


def test_overlong_sentence_is_split_on_words():
    text = " ".join(f"w{n}" for n in range(250)) + "."
    chunks = chunk_section("s", [Segment(1, text)], words_tokens, min_tokens=60, max_tokens=100, overlap_tokens=10)
    assert all(chunk.token_count <= 100 for chunk in chunks)
    assert " ".join(chunk.text for chunk in chunks).count("w249.") >= 1


@pytest.mark.parametrize("bounds", [(50, 100, 50), (120, 100, 10), (60, 100, -1)])
def test_chunk_bounds_are_validated(bounds):
    minimum, maximum, overlap = bounds
    with pytest.raises(ValueError):
        chunk_section("s", [Segment(1, "x")], words_tokens, min_tokens=minimum, max_tokens=maximum,
                      overlap_tokens=overlap)


# PDF extraction and catalog

def test_sections_split_on_headings_and_cite_printed_pages(tmp_path):
    paths = write_inputs(tmp_path)
    spec = load_catalog(paths["sources"]).sources[0]
    document = SourceDocument(spec, paths["corpus"] / "book.pdf")

    leaves = document.section("1.1 Leaves Make Food")
    assert [segment.page for segment in leaves.segments] == [10, 11, 12]  # opener page inferred as 10
    leaf_text = " ".join(segment.text for segment in leaves.segments)
    assert leaf_text.startswith("Fact 0 links") and "Fact 69 links" in leaf_text
    assert "Chapter Outline" not in leaf_text and "Kidneys" not in leaf_text and "Page 1" not in leaf_text

    kidneys = document.section("1.2 Kidneys Filter Blood")  # heading wrapped over two lines
    kidney_text = " ".join(segment.text for segment in kidneys.segments)
    assert [segment.page for segment in kidneys.segments] == [12, 13]
    assert kidney_text.startswith("Fact 0 links kidney") and "chlorophyll" not in kidney_text
    assert "glossary" not in kidney_text
    assert document.stats.heading_misses == [] and document.stats.page_label_fallbacks == set()
    assert document.stats.pdf_pages == {0, 1, 2, 3}


def test_unknown_section_is_rejected(tmp_path):
    paths = write_inputs(tmp_path, sections=["9.9 Missing Section"])
    with pytest.raises(IngestError, match="not found"):
        build(paths)


def test_pdf_checksum_and_offline_rules(tmp_path):
    paths = write_inputs(tmp_path)
    spec = load_catalog(paths["sources"]).sources[0]
    assert ensure_pdf(spec, paths["corpus"], offline=True) == paths["corpus"] / "book.pdf"
    (paths["corpus"] / "book.pdf").write_bytes(b"%PDF-1.4 tampered")
    with pytest.raises(IngestError, match="SHA-256"):
        ensure_pdf(spec, paths["corpus"], offline=True)
    (paths["corpus"] / "book.pdf").unlink()
    with pytest.raises(IngestError, match="downloads are disabled"):
        ensure_pdf(spec, paths["corpus"], offline=True)


@pytest.mark.parametrize("change", [
    {"file": "../escape.pdf"},
    {"download_url": "http://example.org/book.pdf"},
    {"line_patterns": ["(unclosed"]},
    {"sections": ["1.1 Leaves Make Food", "1.1 Leaves Make Food"]},
    {"sha256": "abc"},
    {"unexpected": True},
])
def test_invalid_catalog_entries_are_rejected(tmp_path, change):
    paths = write_inputs(tmp_path)
    data = yaml.safe_load(paths["sources"].read_text())
    data["sources"][0].update(change)
    paths["sources"].write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        load_catalog(paths["sources"])


def test_topic_override_must_name_a_syllabus_topic(tmp_path):
    paths = write_inputs(tmp_path, sections=[{"title": "1.1 Leaves Make Food", "topic": "genetics"}])
    with pytest.raises(ValueError, match="unknown topic"):
        build(paths)


def test_repository_catalog_and_syllabus_are_valid():
    root = Path(__file__).parents[1]
    catalog = load_catalog(root / "ingest/sources.yaml")
    syllabus = load_syllabus(root / "data/syllabus_jamb_bio.json")
    assert len(syllabus.topics) == 10 and syllabus.goal.code == "jamb_bio"
    assert all(source.license and source.url and source.sha256 for source in catalog.sources)
    from ingest.catalog import check_topic_overrides
    check_topic_overrides(catalog, syllabus)


# Corpus building and topic mapping

def test_corpus_maps_chunks_to_most_similar_topic(tmp_path):
    corpus = build(write_inputs(tmp_path))
    by_section = {chunk.section: chunk.topic_code for chunk in corpus.chunks}
    assert by_section == {"1.1 Leaves Make Food": "photosynthesis", "1.2 Kidneys Filter Blood": "excretion"}
    assert all(chunk.token_count <= ingest.CHUNK_MAX_TOKENS for chunk in corpus.chunks)
    assert len({chunk.id for chunk in corpus.chunks}) == len(corpus.chunks)
    assert build(write_inputs(tmp_path)).chunks[0].id == corpus.chunks[0].id  # stable across runs
    report = format_report(corpus)
    assert "photosynthesis" in report and "Spot check" in report and "4 pages" in report


def test_section_override_pins_topic(tmp_path):
    corpus = build(write_inputs(tmp_path, sections=[
        {"title": "1.1 Leaves Make Food", "topic": "excretion"}, "1.2 Kidneys Filter Blood"]))
    assert {chunk.topic_code for chunk in corpus.chunks} == {"excretion"}
    assert all(chunk.pinned == (chunk.section == "1.1 Leaves Make Food") for chunk in corpus.chunks)


def test_coverage_problems_flag_sparse_topics(tmp_path):
    corpus = build(write_inputs(tmp_path))
    assert coverage_problems(corpus, 1) == []
    assert any("excretion" in problem for problem in coverage_problems(corpus, 50))
    corpus.chunks[0].token_count = 10  # first of two chunks in its section, so not section-final
    assert any("Non-final chunk" in problem for problem in coverage_problems(corpus, 1))


# Persistence

def test_persist_is_idempotent_and_keeps_citation_metadata(sessions, clean_corpus, tmp_path):
    corpus = build(write_inputs(tmp_path))
    with sessions.begin() as session:
        first = persist(session, corpus, "syllabus.json")
    with sessions.begin() as session:
        second = persist(session, corpus, "syllabus.json")
    assert (first.inserted, first.deleted) == (len(corpus.chunks), 0)
    assert (second.inserted, second.updated, second.deleted) == (0, len(corpus.chunks), 0)

    with sessions() as session:
        goal = session.scalar(select(Goal).where(Goal.code == "test_bio"))
        topics = session.scalars(select(SyllabusTopic).order_by(SyllabusTopic.order_index)).all()
        assert [(topic.order_index, topic.title) for topic in topics] == [(1, "Photosynthesis"), (2, "Excretion")]
        assert goal.syllabus_ref == "syllabus.json" and goal.exam_date is None
        source = session.scalar(select(Source))
        assert source.license == "CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/)"
        stored = session.scalars(select(Chunk)).all()
        assert {chunk.id for chunk in stored} == {chunk.id for chunk in corpus.chunks}
        for chunk in stored:
            assert chunk.source_id == source.id and chunk.section and chunk.page in {10, 11, 12, 13}
            assert len(chunk.embedding) == embeddings.EMBEDDING_DIMENSION
            assert chunk.syllabus_topic_id in {topic.id for topic in topics}


def test_persist_reorders_topics_and_prunes_removed_content(sessions, clean_corpus, tmp_path):
    with sessions.begin() as session:
        persist(session, build(write_inputs(tmp_path / "a")), "syllabus.json")
    swapped = [{"code": "excretion", "title": "Excretion", "objectives": KIDNEY_WORDS},
               {"code": "photosynthesis", "title": "Photosynthesis", "objectives": LEAF_WORDS}]
    smaller = build(write_inputs(tmp_path / "b", key="renamed-book", sections=["1.2 Kidneys Filter Blood"],
                                 topics=swapped))
    with sessions.begin() as session:
        stats = persist(session, smaller, "syllabus.json")
    assert stats.inserted == len(smaller.chunks) and stats.deleted > 0
    with sessions() as session:
        assert session.scalars(select(Source.title)).all() == ["Synthetic Biology"]
        assert session.scalar(select(func.count()).select_from(Chunk)) == len(smaller.chunks)
        assert session.scalars(select(SyllabusTopic.title).order_by(SyllabusTopic.order_index)).all() == [
            "Excretion", "Photosynthesis"]


def test_persist_refuses_to_remove_cited_chunks(sessions, clean_corpus, tmp_path):
    corpus = build(write_inputs(tmp_path / "a"))
    with sessions.begin() as session:
        persist(session, corpus, "syllabus.json")
        topic = session.scalar(select(SyllabusTopic))
        now = datetime(2026, 1, 1, tzinfo=UTC)
        circle = Circle(goal_id=topic.goal_id, name="Demo", start_date=date(2026, 1, 1), created_at=now)
        session.add(circle)
        session.flush()
        session.add(Lesson(circle_id=circle.id, day_index=1, topic_id=topic.id, body={"en": "lesson"},
                           cited_chunk_ids=[str(corpus.chunks[0].id)], posted_at=now))

    smaller = build(write_inputs(tmp_path / "b", sections=["1.2 Kidneys Filter Blood"]))
    with pytest.raises(IngestError, match="cited by lessons"), sessions.begin() as session:
        persist(session, smaller, "syllabus.json")
    with sessions() as session:  # the failed run rolled back
        assert session.scalar(select(func.count()).select_from(Chunk)) == len(corpus.chunks)


def test_persist_drops_pregenerated_lessons_citing_removed_chunks(sessions, clean_corpus, tmp_path):
    corpus = build(write_inputs(tmp_path / "a"))
    smaller = build(write_inputs(tmp_path / "b", sections=["1.2 Kidneys Filter Blood"]))
    kept, removed = smaller.chunks[0].id, (set(c.id for c in corpus.chunks) - {c.id for c in smaller.chunks}).pop()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with sessions.begin() as session:
        persist(session, corpus, "syllabus.json")
        first, second = session.scalars(select(SyllabusTopic).order_by(SyllabusTopic.order_index)).all()[:2]
        session.add_all([PregeneratedLesson(topic_id=first.id, content={}, cited_chunk_ids=[removed], created_at=now),
                         PregeneratedLesson(topic_id=second.id, content={}, cited_chunk_ids=[kept], created_at=now)])
    with sessions.begin() as session:
        stats = persist(session, smaller, "syllabus.json")
    assert stats.pregenerated_removed == 1
    with sessions() as session:
        assert [row.cited_chunk_ids for row in session.scalars(select(PregeneratedLesson))] == [[kept]]


def test_persist_rejects_a_second_goal_without_touching_the_first(sessions, clean_corpus, tmp_path):
    original = build(write_inputs(tmp_path / "a"))
    with sessions.begin() as session:
        persist(session, original, "syllabus.json")
    other = build(write_inputs(tmp_path / "b", key="other-book", goal_code="waec_bio"))
    with pytest.raises(IngestError, match="supports one goal"), sessions.begin() as session:
        persist(session, other, "other.json")
    with sessions() as session:
        assert session.scalars(select(Goal.code)).all() == ["test_bio"]
        assert {chunk.id for chunk in session.scalars(select(Chunk))} == {chunk.id for chunk in original.chunks}


def test_persist_leaves_content_outside_its_scope(sessions, clean_corpus, tmp_path):
    with sessions.begin() as session:
        manual = Source(title="Manual", publisher="Teacher", license="CC0", url="https://example.org/m",
                        file_path="manual.pdf")
        session.add(manual)
        session.flush()
        session.add(Chunk(source_id=manual.id, section="Notes", page=1, text="Unmapped notes",
                          embedding=[0.0] * embeddings.EMBEDDING_DIMENSION))
    corpus = build(write_inputs(tmp_path))
    with sessions.begin() as session:
        stats = persist(session, corpus, "syllabus.json")
    assert stats.deleted == 0
    with sessions() as session:
        assert set(session.scalars(select(Source.title))) == {"Manual", "Synthetic Biology"}
        assert session.scalar(select(func.count()).select_from(Chunk)) == len(corpus.chunks) + 1


# Command line

@pytest.fixture
def cli_environment(monkeypatch, database_url):
    monkeypatch.setenv("HASH_SALT", SALT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(embeddings, "E5Embedder", lambda model_name: FakeEmbedder())


def cli_args(paths, *extra):
    return ["--sources", str(paths["sources"]), "--syllabus", str(paths["syllabus"]),
            "--corpus-dir", str(paths["corpus"]), "--offline", "--min-chunks-per-topic", "1", *extra]


def test_cli_writes_corpus_and_report(sessions, clean_corpus, cli_environment, tmp_path, capsys):
    paths = write_inputs(tmp_path)
    report = tmp_path / "report.md"
    assert ingest.main(cli_args(paths, "--report", str(report))) == 0
    assert "Database updated" in capsys.readouterr().out
    assert "## Topics" in report.read_text()
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Chunk)) > 0


@pytest.mark.parametrize("extra, code", [(["--dry-run"], 0), (["--min-chunks-per-topic", "50"], 1)])
def test_cli_does_not_write_on_dry_run_or_sparse_topics(sessions, clean_corpus, cli_environment, tmp_path,
                                                        extra, code):
    assert ingest.main(cli_args(write_inputs(tmp_path), *extra)) == code
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Chunk)) == 0


def test_cli_reports_missing_files(cli_environment, tmp_path, capsys):
    paths = write_inputs(tmp_path)
    (paths["corpus"] / "book.pdf").unlink()
    assert ingest.main(cli_args(paths)) == 2
    assert "downloads are disabled" in capsys.readouterr().err
