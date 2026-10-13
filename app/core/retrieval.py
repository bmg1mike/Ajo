from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.embeddings import Embedder
from app.models import Chunk, Source, SyllabusTopic

TOP_K = 8


@dataclass(frozen=True)
class ChunkRef:
    """A library chunk with what a citation needs."""
    id: UUID
    source_title: str
    section: str
    page: int
    text: str

    @property
    def citation(self) -> str:
        return f"{self.source_title}, {self.section}, p. {self.page}"


@dataclass(frozen=True)
class RetrievedChunk(ChunkRef):
    score: float  # cosine similarity; e5 vectors are normalized


def retrieve(session: Session, embedder: Embedder, goal_id: UUID, query: str, *,
             limit: int = TOP_K, topic_id: UUID | None = None) -> list[RetrievedChunk]:
    """Return the `limit` chunks most similar to `query` within one goal's syllabus, best first.

    With `topic_id`, only that syllabus topic's chunks are candidates.
    """
    vector = embedder.embed_queries([query])[0]
    in_goal = (select(Chunk, Source.title)
               .join(Source, Source.id == Chunk.source_id)
               .join(SyllabusTopic, SyllabusTopic.id == Chunk.syllabus_topic_id)
               .where(SyllabusTopic.goal_id == goal_id))
    if topic_id is not None:
        in_goal = in_goal.where(Chunk.syllabus_topic_id == topic_id)
    if session.get_bind().dialect.name == "postgresql":
        distance = Chunk.embedding.cosine_distance(vector)
        rows = session.execute(in_goal.add_columns(distance).order_by(distance, Chunk.id).limit(limit)).all()
        scored = [(chunk, title, 1.0 - float(dist)) for chunk, title, dist in rows]
    else:
        # SQLite has no vector index; an exact scan is fine for a test-sized corpus.
        scored = [(chunk, title, sum(a * b for a, b in zip(chunk.embedding, vector, strict=True)))
                  for chunk, title in session.execute(in_goal)]
        scored.sort(key=lambda row: (-row[2], row[0].id))
        scored = scored[:limit]
    return [RetrievedChunk(chunk.id, title, chunk.section, chunk.page, chunk.text, score)
            for chunk, title, score in scored]


def load_chunks(session: Session, chunk_ids: Iterable[UUID]) -> list[ChunkRef]:
    """The given chunks in the given order, skipping duplicates and IDs that no longer exist."""
    ids = list(dict.fromkeys(chunk_ids))
    if not ids:
        return []
    rows = session.execute(select(Chunk.id, Source.title, Chunk.section, Chunk.page, Chunk.text)
                           .join(Source, Source.id == Chunk.source_id).where(Chunk.id.in_(ids))).all()
    by_id = {row.id: ChunkRef(row.id, row.title, row.section, row.page, row.text) for row in rows}
    return [by_id[chunk_id] for chunk_id in ids if chunk_id in by_id]
