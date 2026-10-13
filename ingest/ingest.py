"""Build the retrieval corpus: PDF sections -> chunks -> embeddings -> topics -> database.

Usage: uv run python -m ingest.ingest --sources ingest/sources.yaml [--dry-run] [--offline]
"""
import argparse
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid5

import yaml
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.embeddings import EMBEDDING_DIMENSION, Embedder
from app.models import Chunk, Goal, Lesson, Question, Source, SyllabusTopic
from ingest.catalog import Catalog, SourceSpec, Syllabus, check_topic_overrides, load_catalog, load_syllabus
from ingest.chunking import chunk_section
from ingest.pdf import ExtractionStats, IngestError, SourceDocument, ensure_pdf

CHUNK_MIN_TOKENS = 300
CHUNK_MAX_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50
# Share of a chunk's topic score taken from its section's mean score.
SECTION_WEIGHT = 0.5
# Stable IDs let re-ingestion update rows in place instead of churning citations.
NAMESPACE = UUID("457524fb-13bc-4ebc-a044-5518207c576c")


def source_id(key: str) -> UUID:
    return uuid5(NAMESPACE, f"source\x00{key}")


def topic_id(goal_code: str, topic_code: str) -> UUID:
    return uuid5(NAMESPACE, f"topic\x00{goal_code}\x00{topic_code}")


def chunk_id(source_key: str, section: str, page: int, text: str) -> UUID:
    return uuid5(NAMESPACE, f"chunk\x00{source_key}\x00{section}\x00{page}\x00{text}")


@dataclass
class PreparedChunk:
    id: UUID
    source_key: str
    section: str
    page: int
    text: str
    token_count: int
    embedding: list[float]
    topic_code: str = ""
    similarity: float = 0.0  # cosine similarity to the assigned topic
    pinned: bool = False  # topic set by a manual override in sources.yaml


@dataclass
class SourceResult:
    spec: SourceSpec
    file_path: str
    stats: ExtractionStats


@dataclass
class Corpus:
    syllabus: Syllabus
    sources: list[SourceResult]
    chunks: list[PreparedChunk]


@dataclass
class PersistStats:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0


def build_corpus(catalog: Catalog, syllabus: Syllabus, corpus_dir: Path, embedder: Embedder, *,
                 offline: bool = False) -> Corpus:
    check_topic_overrides(catalog, syllabus)
    sources: list[SourceResult] = []
    chunks: list[PreparedChunk] = []
    pins: dict[UUID, str] = {}
    seen: set[UUID] = set()
    for spec in catalog.sources:
        path = ensure_pdf(spec, corpus_dir, offline=offline)
        document = SourceDocument(spec, path)
        for section_spec in spec.sections:
            section = document.section(section_spec.title)
            for draft in chunk_section(section.title, section.segments, embedder.count_tokens,
                                       min_tokens=CHUNK_MIN_TOKENS, max_tokens=CHUNK_MAX_TOKENS,
                                       overlap_tokens=CHUNK_OVERLAP_TOKENS):
                identifier = chunk_id(spec.key, draft.section, draft.page, draft.text)
                if identifier in seen:
                    continue  # same text, section and page: an exact duplicate
                seen.add(identifier)
                chunks.append(PreparedChunk(identifier, spec.key, draft.section, draft.page, draft.text,
                                            draft.token_count, embedding=[]))
                if section_spec.topic:
                    pins[identifier] = section_spec.topic
        sources.append(SourceResult(spec, (corpus_dir / spec.file).as_posix(), document.stats))

    embeddings = embedder.embed_passages([chunk.text for chunk in chunks])
    for chunk, embedding in zip(chunks, embeddings, strict=True):
        if len(embedding) != EMBEDDING_DIMENSION:
            raise IngestError(f"Embedding has {len(embedding)} dimensions; expected {EMBEDDING_DIMENSION}")
        chunk.embedding = embedding
    assign_topics(chunks, syllabus, embedder, pins)
    return Corpus(syllabus, sources, chunks)


def assign_topics(chunks: Sequence[PreparedChunk], syllabus: Syllabus, embedder: Embedder,
                  pins: dict[UUID, str]) -> None:
    """Assign each chunk a topic by cosine similarity, unless a section override pins it.

    e5 similarities are compressed into a narrow band, so a chunk's own score is blended
    with its section's mean score: sections share a topic unless a chunk clearly differs.
    """
    topics = syllabus.topics
    vectors = embedder.embed_queries([f"{topic.title}. {topic.objectives}" for topic in topics])
    scores = [{topic.code: _dot(chunk.embedding, vector) for topic, vector in zip(topics, vectors, strict=True)}
              for chunk in chunks]
    by_section: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, chunk in enumerate(chunks):
        by_section[(chunk.source_key, chunk.section)].append(index)
    for members in by_section.values():
        section_mean = {topic.code: statistics.fmean(scores[i][topic.code] for i in members) for topic in topics}
        for index in members:
            chunk = chunks[index]
            blended = {code: (1 - SECTION_WEIGHT) * score + SECTION_WEIGHT * section_mean[code]
                       for code, score in scores[index].items()}
            chunk.pinned = chunk.id in pins
            chunk.topic_code = pins[chunk.id] if chunk.pinned else max(blended, key=blended.__getitem__)
            chunk.similarity = scores[index][chunk.topic_code]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def coverage_problems(corpus: Corpus, min_chunks: int) -> list[str]:
    counts = Counter(chunk.topic_code for chunk in corpus.chunks)
    problems = [f"Topic {topic.code!r} has {counts[topic.code]} chunks; at least {min_chunks} required"
                for topic in corpus.syllabus.topics if counts[topic.code] < min_chunks]
    final = _section_final_ids(corpus.chunks)
    problems.extend(f"Non-final chunk in {chunk.section!r} (p. {chunk.page}) has {chunk.token_count} tokens; "
                    f"at least {CHUNK_MIN_TOKENS} required"
                    for chunk in corpus.chunks if chunk.token_count < CHUNK_MIN_TOKENS and chunk.id not in final)
    return problems


def _section_final_ids(chunks: Sequence[PreparedChunk]) -> set[UUID]:
    last: dict[tuple[str, str], UUID] = {}
    for chunk in chunks:
        last[(chunk.source_key, chunk.section)] = chunk.id
    return set(last.values())


def format_report(corpus: Corpus, *, spot_check: int = 3) -> str:
    lines = ["# Corpus ingestion report", "", "## Sources"]
    by_source = Counter(chunk.source_key for chunk in corpus.chunks)
    for source in corpus.sources:
        stats = source.stats
        lines.append(f"- {source.spec.key}: {len(source.spec.sections)} sections, {len(stats.pdf_pages)} pages, "
                     f"{by_source[source.spec.key]} chunks ({source.spec.license})")
        for miss in stats.heading_misses:
            lines.append(f"  warning: section boundary not detected, {miss}")
        if stats.page_label_fallbacks:
            lines.append(f"  printed page not found on pdf pages {sorted(i + 1 for i in stats.page_label_fallbacks)};"
                         " used PDF page labels")
    total_pages = sum(len(source.stats.pdf_pages) for source in corpus.sources)
    tokens = [chunk.token_count for chunk in corpus.chunks]
    lines += ["", f"Total: {total_pages} pages, {len(corpus.chunks)} chunks"]
    if tokens:
        short = [chunk for chunk in corpus.chunks if chunk.token_count < CHUNK_MIN_TOKENS]
        final_ids = _section_final_ids(corpus.chunks)
        final = sum(chunk.id in final_ids for chunk in short)
        lines.append(f"Tokens per chunk: min {min(tokens)}, median {statistics.median(tokens):g}, max {max(tokens)}; "
                     f"{len(short)} below {CHUNK_MIN_TOKENS} ({final} section-final, {len(short) - final} other)")

    lines += ["", "## Topics"]
    by_topic: dict[str, list[PreparedChunk]] = defaultdict(list)
    for chunk in corpus.chunks:
        by_topic[chunk.topic_code].append(chunk)
    for index, topic in enumerate(corpus.syllabus.topics, start=1):
        assigned = by_topic[topic.code]
        mean = statistics.fmean(chunk.similarity for chunk in assigned) if assigned else 0.0
        lines.append(f"{index:>2}. {topic.code}: {len(assigned)} chunks, mean similarity {mean:.3f}")

    lines += ["", "## Section mapping (majority topic and share of chunks)"]
    sections: dict[tuple[str, str], list[PreparedChunk]] = defaultdict(list)
    for chunk in corpus.chunks:
        sections[(chunk.source_key, chunk.section)].append(chunk)
    for (key, section), members in sections.items():
        topic, count = Counter(chunk.topic_code for chunk in members).most_common(1)[0]
        pinned = " (pinned)" if members[0].pinned else ""
        lines.append(f"- {key} | {section} -> {topic} {count}/{len(members)}{pinned}")

    if spot_check:
        lines += ["", f"## Spot check: {spot_check} least similar chunks per topic"]
        for topic in corpus.syllabus.topics:
            for chunk in sorted(by_topic[topic.code], key=lambda item: item.similarity)[:spot_check]:
                excerpt = chunk.text[:140].replace("\n", " ")
                lines.append(f"- {topic.code} {chunk.similarity:.3f} [{chunk.section}, p. {chunk.page}] {excerpt}...")
    return "\n".join(lines)


def persist(session: Session, corpus: Corpus, syllabus_ref: str) -> PersistStats:
    """Make the database match the corpus inside the caller's transaction.

    The POC supports a single goal, so a database holding any other goal is rejected
    before anything is written. Cleanup is scoped to this goal's topics and the
    catalog's sources: stale chunks there are removed, but never chunks that lessons
    or questions cite (regenerate those or reset the demo first). A source outside the
    catalog is removed only once no chunks reference it.
    """
    syllabus = corpus.syllabus
    stats = PersistStats()

    other_goals = session.scalars(select(Goal.code).where(Goal.code != syllabus.goal.code)).all()
    if other_goals:
        raise IngestError(f"The database already holds goal(s) {', '.join(sorted(other_goals))}; this POC supports "
                          f"one goal, so ingesting {syllabus.goal.code!r} is refused. Remove the other goal first.")

    goal = session.scalar(select(Goal).where(Goal.code == syllabus.goal.code))
    if goal is None:
        goal = Goal(code=syllabus.goal.code)
        session.add(goal)
    goal.title, goal.country = syllabus.goal.title, syllabus.goal.country
    goal.exam_date, goal.syllabus_ref = syllabus.goal.exam_date, syllabus_ref
    session.flush()

    # Park existing order indexes out of range so reordering cannot trip the unique constraint.
    existing_topics = {topic.id: topic for topic in session.scalars(
        select(SyllabusTopic).where(SyllabusTopic.goal_id == goal.id))}
    for topic in existing_topics.values():
        topic.order_index = -topic.order_index - 1
    session.flush()
    topic_ids = {}
    for order_index, spec in enumerate(syllabus.topics, start=1):
        identifier = topic_id(syllabus.goal.code, spec.code)
        topic = existing_topics.get(identifier) or SyllabusTopic(id=identifier, goal_id=goal.id)
        topic.order_index, topic.title, topic.objectives = order_index, spec.title, spec.objectives
        session.add(topic)
        topic_ids[spec.code] = identifier
    session.flush()

    source_ids = {}
    for result in corpus.sources:
        spec = result.spec
        identifier = source_id(spec.key)
        source = session.get(Source, identifier) or Source(id=identifier)
        source.title, source.publisher, source.url = spec.title, spec.publisher, str(spec.url)
        source.license = f"{spec.license} ({spec.license_url})"
        source.file_path = result.file_path
        session.add(source)
        source_ids[spec.key] = identifier
    session.flush()

    desired = {chunk.id for chunk in corpus.chunks}
    in_scope = or_(Chunk.source_id.in_(source_ids.values()), Chunk.syllabus_topic_id.in_(list(existing_topics)))
    existing_chunks = {chunk.id: chunk for chunk in session.scalars(select(Chunk).where(in_scope))}
    stale = set(existing_chunks) - desired
    cited = stale & _cited_chunk_ids(session)
    if cited:
        raise IngestError(f"{len(cited)} chunks that would be removed are cited by lessons or questions; "
                          "regenerate those lessons or reset the demo before re-ingesting")
    for identifier in stale:
        session.delete(existing_chunks[identifier])
    stats.deleted = len(stale)
    session.flush()

    for prepared in corpus.chunks:
        chunk = existing_chunks.get(prepared.id)
        if chunk is None:
            chunk = Chunk(id=prepared.id)
            session.add(chunk)
            stats.inserted += 1
        else:
            stats.updated += 1
        chunk.source_id, chunk.section, chunk.page = source_ids[prepared.source_key], prepared.section, prepared.page
        chunk.text, chunk.embedding = prepared.text, prepared.embedding
        chunk.syllabus_topic_id = topic_ids[prepared.topic_code]
    session.flush()

    unreferenced = ~select(Chunk.id).where(Chunk.source_id == Source.id).exists()
    for source in session.scalars(select(Source).where(Source.id.not_in(source_ids.values()), unreferenced)):
        session.delete(source)
    for identifier, topic in existing_topics.items():
        if identifier not in topic_ids.values():
            session.delete(topic)  # fails on the RESTRICT key if lessons still use the topic
    session.flush()
    return stats


def _cited_chunk_ids(session: Session) -> set[UUID]:
    cited: set[UUID] = set()
    for column in (Lesson.cited_chunk_ids, Question.cited_chunk_ids):
        for ids in session.scalars(select(column)):
            cited.update(UUID(str(value)) for value in ids or ())
    return cited


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", type=Path, default=Path("ingest/sources.yaml"))
    parser.add_argument("--syllabus", type=Path, default=Path("data/syllabus_jamb_bio.json"))
    parser.add_argument("--corpus-dir", type=Path, default=Path("data/corpus"))
    parser.add_argument("--offline", action="store_true", help="fail instead of downloading missing PDFs")
    parser.add_argument("--dry-run", action="store_true", help="build and report without writing to the database")
    parser.add_argument("--min-chunks-per-topic", type=int, default=5)
    parser.add_argument("--spot-check", type=int, default=3, help="low-similarity samples to print per topic")
    parser.add_argument("--report", type=Path, help="also write the report to this file")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # Imported here so `--help` and tests do not need a configured environment or torch.
    from app.config import Settings
    from app.core.embeddings import E5Embedder
    from app.db import build_engine, session_factory

    try:
        settings = Settings()
        catalog, syllabus = load_catalog(args.sources), load_syllabus(args.syllabus)
        corpus = build_corpus(catalog, syllabus, args.corpus_dir, E5Embedder(settings.embed_model),
                              offline=args.offline)
        report = format_report(corpus, spot_check=args.spot_check)
        print(report)
        if args.report:
            args.report.write_text(report + "\n", encoding="utf-8")
        problems = coverage_problems(corpus, args.min_chunks_per_topic)
        if problems:
            print("\nNot written to the database:", *problems, sep="\n- ", file=sys.stderr)
            return 1
        if args.dry_run:
            print("\nDry run: database not modified.")
            return 0
        engine = build_engine(settings.database_url)
        try:
            with session_factory(engine).begin() as session:
                stats = persist(session, corpus, args.syllabus.as_posix())
        finally:
            engine.dispose()
    except (IngestError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        return 2
    print(f"\nDatabase updated: {stats.inserted} chunks inserted, {stats.updated} updated, {stats.deleted} removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
