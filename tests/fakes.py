import hashlib
import json
import math
import re
from uuid import uuid4

import httpx

from app.core import embeddings
from app.models import Chunk, Goal, Source, SyllabusTopic


class FakeEmbedder:
    """Whitespace tokens and hashed bag-of-words vectors: deterministic and model-free."""

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def embed_passages(self, texts):
        return [self._vector(text) for text in texts]

    embed_queries = embed_passages

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * embeddings.EMBEDDING_DIMENSION
        for word in re.findall(r"[a-z]+", text.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % len(vector)] += 1
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def chat(content: str, status: int = 200, **headers) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"role": "assistant", "content": content}}]},
                          headers=headers)


class FakeLLM:
    """Scripted chat completions behind the real client; records every request body."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if not self.responses:
            raise AssertionError("unexpected LLM call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def add_goal_with_chunks(session, texts: list[str]) -> tuple[Goal, list[Chunk]]:
    goal = Goal(code=f"test_{uuid4().hex}", title="Synthetic Biology", syllabus_ref="synthetic.json")
    source = Source(title="Synthetic Book", publisher="Test Press", license="CC0", url="https://example.org",
                    file_path="book.pdf")
    session.add_all([goal, source])
    session.flush()
    topic = SyllabusTopic(goal_id=goal.id, order_index=1, title="Living things", objectives="Synthetic")
    session.add(topic)
    session.flush()
    chunks = [Chunk(source_id=source.id, section=f"1.{index} Section", page=10 + index, text=text,
                    embedding=FakeEmbedder().embed_passages([text])[0], syllabus_topic_id=topic.id)
              for index, text in enumerate(texts, start=1)]
    session.add_all(chunks)
    session.flush()
    return goal, chunks
