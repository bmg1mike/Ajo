"""Milestone 1 completion check against the real corpus and embedding model.

Opt-in because it needs the downloaded PDFs (see ingest/sources.yaml) and the local
model, and takes about a minute: AJO_CORPUS_TESTS=1 uv run pytest -m corpus
"""
import os
from pathlib import Path

import pytest

from ingest.catalog import load_catalog, load_syllabus
from ingest.ingest import CHUNK_MAX_TOKENS, CHUNK_MIN_TOKENS, build_corpus, coverage_problems

ROOT = Path(__file__).parents[1]

pytestmark = [
    pytest.mark.corpus,
    pytest.mark.skipif(os.getenv("AJO_CORPUS_TESTS") != "1", reason="set AJO_CORPUS_TESTS=1 to ingest the real corpus"),
]


def test_real_corpus_meets_milestone_one():
    from app.core.embeddings import E5Embedder

    catalog = load_catalog(ROOT / "ingest/sources.yaml")
    syllabus = load_syllabus(ROOT / "data/syllabus_jamb_bio.json")
    corpus = build_corpus(catalog, syllabus, ROOT / "data/corpus",
                          E5Embedder("intfloat/multilingual-e5-small"), offline=True)

    pages = sum(len(source.stats.pdf_pages) for source in corpus.sources)
    assert 180 <= pages <= 250
    for source in corpus.sources:
        assert source.stats.heading_misses == []
        assert source.stats.page_label_fallbacks == set()
    counts = {topic.code: 0 for topic in syllabus.topics}
    for chunk in corpus.chunks:
        counts[chunk.topic_code] += 1
        assert chunk.section and chunk.page > 0 and chunk.token_count <= CHUNK_MAX_TOKENS
    assert min(counts.values()) >= 5, counts
    # Includes the rule that only a section's final chunk may fall below the minimum.
    assert coverage_problems(corpus, 5) == []
    last_in_section = {(chunk.source_key, chunk.section): chunk.id for chunk in corpus.chunks}
    assert all(chunk.token_count >= CHUNK_MIN_TOKENS for chunk in corpus.chunks
               if chunk.id not in last_in_section.values())
