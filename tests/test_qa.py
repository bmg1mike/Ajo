import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.core import qa_eval
from app.core.embeddings import LazyEmbedder
from app.core.privacy import channel_hash, delete_learner, redact_contact_details
from app.core.qa import (
    ASK_CIRCLE, DISMISS, EXCERPT_CHARS, SHOW_SOURCE, FallbackReason, QAService, ask_circle, excerpt, show_source,
)
from app.core.retrieval import retrieve
from app.llm import prompts
from app.llm.client import LLMClient, LLMError, LLMUnavailable
from app.main import create_app
from app.models import (
    Channel, Chunk, Circle, CircleMember, FeedEvent, FeedType, Goal, Language, Learner, LLMCache, QALog, Source,
    SyllabusTopic,
)
from tests.fakes import FakeEmbedder

ROOT = Path(__file__).parents[1]
SALT = "synthetic-test-salt-with-32-characters"
LEAF = "Leaves use chlorophyll to capture light. Leaf cells turn carbon dioxide and water into sugar and glucose."
KIDNEY = "The kidney filters blood in each nephron. Urea and salt leave the body in urine stored in the bladder."
LEAF_QUESTION = "How do leaf cells use chlorophyll and light to make sugar?"
EMBEDDER = FakeEmbedder()
MIN_SCORE = 0.3  # bag-of-words similarities are lower than e5's


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


@pytest.fixture
def make_client(sessions, clock):
    clients = []

    def make(fake: FakeLLM | None = None, **options) -> LLMClient:
        defaults = dict(sessions=sessions, clock=clock, base_url="https://llm.example/v1/", api_key="test-key",
                        model="test-model", enabled=True, cache=False, transport=httpx.MockTransport(fake or FakeLLM()),
                        sleep=options.pop("sleep", lambda seconds: None))
        client = LLMClient(**(defaults | options))
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


@dataclass
class Corpus:
    goal_id: UUID
    learner: Learner
    leaf: Chunk
    kidney: Chunk
    other_goal_chunk: Chunk


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
                    embedding=EMBEDDER.embed_passages([text])[0], syllabus_topic_id=topic.id)
              for index, text in enumerate(texts, start=1)]
    session.add_all(chunks)
    session.flush()
    return goal, chunks


@pytest.fixture
def corpus(sessions, clock):
    now = clock.now()
    with sessions.begin() as session:
        goal, (leaf, kidney) = add_goal_with_chunks(session, [LEAF, KIDNEY])
        # The same leaf text in another goal must never be retrieved for this learner.
        _, (other,) = add_goal_with_chunks(session, [LEAF])
        learner = Learner(channel=Channel.TELEGRAM, channel_user_hash=channel_hash(Channel.TELEGRAM, uuid4().hex, SALT),
                          channel_chat_id=f"chat{uuid4().hex[:12]}", first_name="Adaeze", goal_id=goal.id,
                          created_at=now)
        session.add(learner)
    return Corpus(goal.id, learner, leaf, kidney, other)


def qa_service(client: LLMClient, clock, min_score: float = MIN_SCORE) -> QAService:
    return QAService(EMBEDDER, client, clock, min_score=min_score)


def ask(sessions, service: QAService, learner: Learner, question: str = LEAF_QUESTION):
    with sessions.begin() as session:
        learner = session.merge(learner)
        result = service.ask(session, learner, question)
    with sessions() as session:
        return result, session.get(QALog, result.qa_log_id)


def answer_json(*cited: str, answer: str = "Leaves capture light with chlorophyll and make sugar.",
                answerable: bool = True) -> str:
    return json.dumps({"answerable": answerable, "answer": answer, "cited_chunk_ids": list(cited)})


# LLM client


def test_client_retries_with_backoff_then_succeeds(make_client):
    sleeps = []
    fake = FakeLLM(chat("", 429, **{"Retry-After": "7"}), httpx.ConnectError("down"), chat("", 503), chat("Hello"))
    client = make_client(fake, sleep=sleeps.append)
    assert client.complete([{"role": "user", "content": "Hi"}], json_output=True) == "Hello"
    assert len(fake.requests) == 4
    assert sleeps[0] == 7  # Retry-After wins over the shorter computed backoff
    assert 2 <= sleeps[1] <= 3 and 4 <= sleeps[2] <= 6  # exponential with jitter
    assert fake.requests[0] == {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}],
                                "response_format": {"type": "json_object"}}


def test_client_stops_on_client_errors_and_after_max_attempts(make_client):
    fake = FakeLLM(chat("", 400))
    with pytest.raises(LLMError, match="HTTP 400"):
        make_client(fake).complete([{"role": "user", "content": "Hi"}])
    assert len(fake.requests) == 1
    fake = FakeLLM(*[chat("", 500)] * 3)
    with pytest.raises(LLMError, match="after 3 attempts"):
        make_client(fake, max_attempts=3).complete([{"role": "user", "content": "Hi"}])
    assert len(fake.requests) == 3


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"unexpected": True}),
    httpx.Response(200, json={"choices": [{"message": {"content": [{"type": "text", "text": "Hi"}]}}]}),
    httpx.Response(200, json={"choices": [{"message": {"content": {"text": "Hi"}}}]}),
    httpx.Response(200, json={"choices": [{"message": {"content": 42}}]}),
    httpx.Response(200, json={"choices": [{"message": None}]}),
    httpx.Response(200, content=b"not json"),
    chat("   "),
    chat("<think>only reasoning</think>"),
])
def test_client_rejects_unusable_responses(make_client, response):
    with pytest.raises(LLMError):
        make_client(FakeLLM(response)).complete([{"role": "user", "content": "Hi"}])


def test_client_strips_reasoning_and_sends_optional_parameters_only_when_set(make_client):
    fake = FakeLLM(chat("<think>hmm</think> yes"), chat("no"))
    assert make_client(fake).complete([{"role": "user", "content": "Hi"}]) == "yes"
    assert "temperature" not in fake.requests[0] and "reasoning_effort" not in fake.requests[0]
    make_client(fake, temperature=0.0, reasoning_effort="low").complete([{"role": "user", "content": "Hi"}])
    assert fake.requests[1]["temperature"] == 0.0 and fake.requests[1]["reasoning_effort"] == "low"


def test_cache_serves_repeats_without_storing_prompts(sessions, make_client):
    secret_prompt = f"Synthetic prompt {uuid4().hex}"
    messages = [{"role": "user", "content": secret_prompt}]
    fake = FakeLLM(chat("Cached answer"), chat("Other model"))
    client = make_client(fake, cache=True)
    assert client.complete(messages) == client.complete(messages) == "Cached answer"
    assert len(fake.requests) == 1
    with sessions() as session:
        key = client.cache_key({"model": "test-model", "messages": messages})
        row = session.get(LLMCache, key)
        assert row.response == {"content": "Cached answer"}
        assert secret_prompt not in json.dumps(row.response) and secret_prompt not in key

    # A rehearsed demo needs no live LLM; an unrehearsed prompt then cannot be answered.
    offline = make_client(FakeLLM(), cache=True, enabled=False)
    assert offline.complete(messages) == "Cached answer"
    with pytest.raises(LLMUnavailable):
        offline.complete([{"role": "user", "content": "never asked"}])
    # Any configuration change is a different cache entry.
    assert make_client(fake, cache=True, model="other-model").complete(messages) == "Other model"
    assert make_client(FakeLLM(chat("Uncached")), cache=False).complete(messages) == "Uncached"
    # A cache entry that is not text is treated as a miss, not returned.
    with sessions.begin() as session:
        session.get(LLMCache, key).response = {"content": ["not", "text"]}
    assert make_client(FakeLLM(chat("Refetched")), cache=True).complete(messages) == "Refetched"


def test_client_reads_settings():
    settings = Settings(_env_file=None, hash_salt=SALT, llm_enabled=True, llm_base_url="https://api.openai.com/v1",
                        llm_api_key="test", llm_model="gpt-test", llm_temperature="", llm_reasoning_effort="low")
    assert settings.llm_temperature is None
    client = LLMClient.from_settings(settings, sessions=None, clock=None)
    try:
        assert client._url == "https://api.openai.com/v1/chat/completions"
        assert client._reasoning_effort == "low" and client._temperature is None
    finally:
        client.close()


# Retrieval


def test_retrieval_ranks_top_k_within_the_learners_goal(sessions, corpus):
    with sessions() as session:
        results = retrieve(session, EMBEDDER, corpus.goal_id, LEAF_QUESTION)
    assert [chunk.id for chunk in results] == [corpus.leaf.id, corpus.kidney.id]
    assert results[0].score > results[1].score
    assert results[0].citation == "Synthetic Book, 1.1 Section, p. 11"

    with sessions.begin() as session:
        topic_id = session.get(Chunk, corpus.leaf.id).syllabus_topic_id
        session.add_all([Chunk(source_id=corpus.leaf.source_id, section="Filler", page=1, text=f"leaf filler {index}",
                               embedding=EMBEDDER.embed_passages([f"leaf filler {index}"])[0],
                               syllabus_topic_id=topic_id) for index in range(10)])
    with sessions() as session:
        ranked = retrieve(session, EMBEDDER, corpus.goal_id, LEAF_QUESTION)
    assert len(ranked) == 8 and ranked[0].id == corpus.leaf.id
    assert [chunk.score for chunk in ranked] == sorted((chunk.score for chunk in ranked), reverse=True)
    assert corpus.other_goal_chunk.id not in {chunk.id for chunk in ranked}


# QA pipeline


def test_accepted_answer_is_cited_rendered_and_logged(sessions, clock, corpus, make_client):
    fake = FakeLLM(chat(answer_json("C1", answer="Leaves capture light with chlorophyll and make sugar. [C1]")),
                   chat("Yes."))
    result, log = ask(sessions, qa_service(make_client(fake), clock), corpus.learner)
    assert result.answered and result.fallback_reason is None
    assert result.reply.text == ("Leaves capture light with chlorophyll and make sugar.\n\n"
                                 "Source: Synthetic Book, 1.1 Section, p. 11")
    assert [(button.text, button.payload) for button in result.reply.buttons] == [
        ("📖 Show source", f"{SHOW_SOURCE}{corpus.leaf.id}")]
    assert all(len(button.payload.encode()) <= 64 for button in result.reply.buttons)
    assert log.answered and log.fallback_reason is None
    assert [UUID(str(value)) for value in log.cited_chunk_ids] == [corpus.leaf.id]
    assert {UUID(str(value)) for value in log.retrieved_chunk_ids} == {corpus.leaf.id, corpus.kidney.id}
    assert log.top_score == pytest.approx(result.retrieved[0].score)
    # The claim check sees the cited chunks only, with the cleaned answer.
    claim_prompt = fake.requests[1]["messages"][1]["content"]
    assert LEAF in claim_prompt and KIDNEY not in claim_prompt and "[C1]" not in claim_prompt.split("<answer>")[1]


def test_multiple_citations_accept_labels_or_ids_and_render_each_source(sessions, clock, corpus, make_client):
    fake = FakeLLM(chat(answer_json("c2", f" {corpus.leaf.id} ", "C2")), chat("yes"))
    result, log = ask(sessions, qa_service(make_client(fake), clock), corpus.learner)
    assert [chunk.id for chunk in result.cited] == [corpus.kidney.id, corpus.leaf.id]
    assert result.reply.text.endswith("Source: Synthetic Book, 1.2 Section, p. 12\n"
                                      "Source: Synthetic Book, 1.1 Section, p. 11")
    assert [button.text for button in result.reply.buttons] == ["📖 Source 1", "📖 Source 2"]


@pytest.mark.parametrize("responses,reason", [
    ([chat("I think leaves make sugar.")], FallbackReason.INVALID_JSON),
    ([chat('{"answerable": "yes", "answer": "x", "cited_chunk_ids": ["C1"]}')], FallbackReason.SCHEMA_MISMATCH),
    ([chat('{"answerable": true, "cited_chunk_ids": ["C1"]}')], FallbackReason.SCHEMA_MISMATCH),
    ([chat(answer_json("C1", answer="  "))], FallbackReason.SCHEMA_MISMATCH),
    ([chat(answer_json(answerable=False, answer=""))], FallbackReason.NOT_ANSWERABLE),
    ([chat(answer_json("C1", answer="word " * 121))], FallbackReason.ANSWER_TOO_LONG),
    ([chat(answer_json())], FallbackReason.NO_CITATIONS),
    ([chat(answer_json("C1", "C3"))], FallbackReason.CITATION_OUTSIDE_RETRIEVED),
    ([chat(answer_json(str(uuid4())))], FallbackReason.CITATION_OUTSIDE_RETRIEVED),
    ([chat(answer_json("C1")), chat("No.")], FallbackReason.UNSUPPORTED_CLAIMS),
    ([chat(answer_json("C1")), chat("**NO**")], FallbackReason.UNSUPPORTED_CLAIMS),
    ([chat(answer_json("C1")), chat("Probably")], FallbackReason.CLAIM_CHECK_ERROR),
    # Qualified or contradictory verdicts are malformed, never a yes.
    ([chat(answer_json("C1", answer="Cells are made of gold.")),
      chat("Yes, the first claim is supported, but the claim about gold is not")], FallbackReason.CLAIM_CHECK_ERROR),
    ([chat(answer_json("C1")), chat("Yes. However, the sugar claim is unsupported.")], FallbackReason.CLAIM_CHECK_ERROR),
    ([chat(answer_json("C1")), chat("yes no")], FallbackReason.CLAIM_CHECK_ERROR),
    ([chat(answer_json("C1")), chat("No, the sugar claim is unsupported.")], FallbackReason.CLAIM_CHECK_ERROR),
    # Malformed provider content is an LLM error, not a crash.
    ([httpx.Response(200, json={"choices": [{"message": {"content": [{"type": "text", "text": "{}"}]}}]})],
     FallbackReason.LLM_ERROR),
    ([chat(answer_json("C1")), httpx.Response(200, json={"choices": [{"message": {"content": {"verdict": "yes"}}}]})],
     FallbackReason.LLM_ERROR),
    ([chat(answer_json("C1")), chat("", 401)], FallbackReason.LLM_ERROR),
    ([chat("", 400)], FallbackReason.LLM_ERROR),
])
def test_invalid_generations_fall_back_and_are_logged(sessions, clock, corpus, make_client, responses, reason):
    fake = FakeLLM(*responses)
    result, log = ask(sessions, qa_service(make_client(fake), clock), corpus.learner)
    assert not fake.responses  # every scripted call was made, and no more
    assert result.fallback_reason == reason and not result.answered and result.cited == []
    assert result.reply.text.startswith("I couldn't find a reliable answer to that in the Ajo library.")
    assert [button.payload for button in result.reply.buttons] == [f"{ASK_CIRCLE}{log.id}", DISMISS]
    assert not log.answered and log.fallback_reason == reason.value and log.cited_chunk_ids is None


@pytest.mark.parametrize("verdict", ["yes", "Yes.", "**YES**", " yes\n", "'Yes'"])
def test_bare_affirmative_verdicts_are_accepted(sessions, clock, corpus, make_client, verdict):
    fake = FakeLLM(chat(answer_json("C1")), chat(verdict))
    result, _ = ask(sessions, qa_service(make_client(fake), clock), corpus.learner)
    assert result.answered


def test_fenced_or_wrapped_json_is_accepted(sessions, clock, corpus, make_client):
    wrapped = f"Here you go:\n```json\n{answer_json('C1')}\n```"
    result, _ = ask(sessions, qa_service(make_client(FakeLLM(chat(wrapped), chat("yes"))), clock), corpus.learner)
    assert result.answered


@pytest.mark.parametrize("question,reason", [
    ("   ", FallbackReason.EMPTY_QUESTION),
    ("leaf " * 300, FallbackReason.QUESTION_TOO_LONG),
    ("Who won the football match yesterday?", FallbackReason.BELOW_THRESHOLD),
])
def test_unanswerable_inputs_never_reach_the_llm(sessions, clock, corpus, make_client, question, reason):
    result, log = ask(sessions, qa_service(make_client(FakeLLM()), clock), corpus.learner, question)
    assert result.fallback_reason == reason and log.fallback_reason == reason.value


def test_learner_without_goal_or_corpus_falls_back(sessions, clock, corpus, make_client):
    service = qa_service(make_client(FakeLLM()), clock)
    with sessions.begin() as session:
        learner = session.merge(corpus.learner)
        learner.goal_id = None
        assert service.ask(session, learner, LEAF_QUESTION).fallback_reason == FallbackReason.NO_GOAL
        empty_goal, _ = add_goal_with_chunks(session, [])
        learner.goal_id = empty_goal.id
        assert service.ask(session, learner, LEAF_QUESTION).fallback_reason == FallbackReason.NO_CHUNKS
        session.rollback()


def test_disabled_llm_without_cache_falls_back(sessions, clock, corpus, make_client):
    result, _ = ask(sessions, qa_service(make_client(FakeLLM(), enabled=False), clock), corpus.learner)
    assert result.fallback_reason == FallbackReason.LLM_UNAVAILABLE


def test_llm_requests_carry_language_and_level_but_no_learner_identity(sessions, clock, corpus, make_client):
    learner = corpus.learner
    with sessions.begin() as session:
        stored = session.get(Learner, learner.id)
        stored.language, stored.difficulty = Language.YO, 3
        learner = stored
    fake = FakeLLM(chat(answer_json("C1")), chat("yes"))
    question = f"I am {learner.first_name}, chat {learner.channel_chat_id}. {LEAF_QUESTION} adaeze?"
    ask(sessions, qa_service(make_client(fake), clock), learner, question)
    payload = json.dumps(fake.requests)
    for identifier in (learner.first_name, learner.first_name.lower(), learner.channel_chat_id, str(learner.id),
                       learner.id.hex, learner.channel_user_hash):
        assert identifier not in payload
    user_prompt = fake.requests[0]["messages"][1]["content"]
    assert "Student language: Yoruba" in user_prompt and "Student level: 3 of 3" in user_prompt
    assert "I am [redacted], chat [redacted]." in user_prompt
    # The local evaluation log keeps the learner's own words, minus anything shaped like contact details.
    with sessions() as session:
        assert (session.scalar(select(QALog.question_text).where(QALog.learner_id == learner.id))
                == redact_contact_details(question))


@pytest.mark.parametrize("phone", ["+2348031234567", "+234 803 123 4567", "08031234567", "0803-123-4567",
                                   "(0803) 123 4567", "(+234) 803.123.4567"])
def test_contact_details_never_reach_the_llm_or_the_log(sessions, clock, corpus, make_client, phone):
    # A USSD learner's phone number is stored only as a hash, so it cannot be matched verbatim.
    with sessions.begin() as session:
        # Unique per case because the hash column is unique across the shared test database.
        learner = Learner(channel=Channel.USSD, channel_user_hash=channel_hash(Channel.USSD, f"+234803{uuid4().hex}", SALT),
                          first_name="Synthetic", goal_id=corpus.goal_id, created_at=clock.now(session))
        session.add(learner)
    fake = FakeLLM(chat(answer_json("C1")), chat("yes"))
    question = f"{LEAF_QUESTION} Text me on {phone} or ada.synthetic@example.com, Telegram ID 5123456789."
    result, log = ask(sessions, qa_service(make_client(fake), clock), learner, question)
    payload = json.dumps(fake.requests, ensure_ascii=False)
    stored = log.question_text
    for text in (payload, stored):
        assert phone not in text and "8031234567" not in text.replace(" ", "").replace("-", "").replace(".", "")
        assert "ada.synthetic@example.com" not in text and "5123456789" not in text
    assert stored == f"{LEAF_QUESTION} Text me on [redacted] or [redacted], Telegram ID [redacted]."
    assert result.answered  # the Biology question itself is untouched


@pytest.mark.parametrize("text,expected", [
    # Several numbers joined by separators form one run longer than any single phone number.
    ("Call 08000000001. 08000000002.", "Call [redacted]."),
    ("Call 08000000001 08000000002", "Call [redacted]"),
    ("Numbers: 08000000001, 08000000002 and 08000000003", "Numbers: [redacted], [redacted] and [redacted]"),
    ("a 12345678901234567890 b", "a [redacted] b"),
    # Digits touching letters, and separators other than a plain space.
    ("Call08031234567 now", "Call[redacted] now"),
    ("ID5123456789x", "ID[redacted]x"),
    ("0803\u00a0123\u00a04567", "[redacted]"),
    ("0803\t123\n4567", "[redacted]"),
    ("0803/123/4567", "[redacted]"),
    ("0803\u2013123\u20134567", "[redacted]"),
    ("0 8 0 3 1 2 3 4 5 6 7", "[redacted]"),
    ("+234 (0) 803 123 4567.", "[redacted]."),
])
def test_contact_redaction_covers_joined_and_unusually_formatted_numbers(text, expected):
    assert redact_contact_details(text) == expected


@pytest.mark.parametrize("text", ["Humans have 46 chromosomes in 23 pairs.", "Ratio 9:3:3:1 in the F2 generation.", "About 1,000,000 nephrons per kidney.",
                                  "Mendel published in 1866.", "Blood pH is 7.35 to 7.45.", "Is 2n = 46 in C4 plants?"])
def test_contact_redaction_keeps_biology_numbers(text):
    assert redact_contact_details(text) == text


def test_prompt_labels_chunks_with_citation_metadata():
    @dataclass
    class Passage:
        source_title: str = "Book"
        section: str = "2.1 Cells"
        page: int = 7
        text: str = "Cells are small."

    messages = prompts.qa_messages("What is a cell?", [Passage()], language="pcm", level=2)
    assert "[C1] Book, 2.1 Cells, p. 7\nCells are small." in messages[1]["content"]
    assert "Student language: Nigerian Pidgin" in messages[1]["content"]
    assert "Use only facts stated in the chunks" in messages[0]["content"]


# Sources and asking the circle


def test_excerpt_truncates_on_a_word_boundary():
    assert excerpt("short   text\n") == "short text"
    long = " ".join(f"word{index}" for index in range(400))
    cut = excerpt(long)
    assert len(cut) <= EXCERPT_CHARS and cut.endswith("…") and long.startswith(cut[:-1])
    assert excerpt("x" * 1000) == "x" * (EXCERPT_CHARS - 1) + "…"


def test_show_source_sends_original_text_within_learners_goal(sessions, corpus):
    with sessions.begin() as session:
        session.get(Chunk, corpus.leaf.id).text = LEAF + " " + "More detail about leaves. " * 50
    with sessions() as session:
        reply = show_source(session, corpus.learner, corpus.leaf.id)
        header, body = reply.text.split("\n\n")
        assert header == "Synthetic Book, 1.1 Section, p. 11"
        assert body.startswith(LEAF) and len(body) <= EXCERPT_CHARS and body.endswith("…")
        for chunk_id in (corpus.other_goal_chunk.id, uuid4()):
            assert show_source(session, corpus.learner, chunk_id).text == "That source is no longer available."


def test_learner_can_post_an_unanswered_question_to_their_circle_once(sessions, clock, corpus, make_client):
    service = qa_service(make_client(FakeLLM()), clock)
    fallback, _ = ask(sessions, service, corpus.learner, "Who won the football match yesterday?")
    now = clock.now()
    with sessions.begin() as session:
        learner = session.merge(corpus.learner)
        assert ask_circle(session, learner, fallback.qa_log_id, now).text.startswith("You're not in a circle")
        circle = Circle(goal_id=corpus.goal_id, name="Demo", start_date=now.date(), created_at=now)
        session.add(circle)
        session.flush()
        session.add(CircleMember(circle_id=circle.id, learner_id=learner.id, joined_at=now))
        session.flush()
        assert ask_circle(session, learner, fallback.qa_log_id, now).text.startswith("Posted.")
        assert ask_circle(session, learner, fallback.qa_log_id, now).text.startswith("You already posted")
        assert ask_circle(session, learner, uuid4(), now).text == "That question can't be posted to your circle."
        events = session.scalars(select(FeedEvent).where(FeedEvent.circle_id == circle.id)).all()
        assert [(event.type, event.actor_learner_id, event.payload["question"]) for event in events] == [
            (FeedType.QUESTION, learner.id, "Who won the football match yesterday?")]

        other = Learner(channel=Channel.WEB, channel_user_hash=channel_hash(Channel.WEB, uuid4().hex, SALT),
                        first_name="Synthetic", goal_id=corpus.goal_id, created_at=now)
        session.add(other)
        session.flush()
        assert ask_circle(session, other, fallback.qa_log_id, now).text == "That question can't be posted to your circle."

    answered_service = qa_service(make_client(FakeLLM(chat(answer_json("C1")), chat("yes"))), clock)
    answered, _ = ask(sessions, answered_service, corpus.learner)
    with sessions.begin() as session:
        learner = session.merge(corpus.learner)
        assert ask_circle(session, learner, answered.qa_log_id, now).text.startswith("That question can't")
        assert delete_learner(session, learner.id, confirmed=True)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(FeedEvent)
                              .where(FeedEvent.circle_id == circle.id)) == 0
        assert session.scalar(select(func.count()).select_from(QALog)
                              .where(QALog.learner_id == corpus.learner.id)) == 0


# Evaluation set and runner


def test_repository_eval_set_matches_the_acceptance_criteria():
    eval_set = qa_eval.load_eval_set(ROOT / qa_eval.DEFAULT_EVAL_SET)
    questions = eval_set.questions
    assert len(questions) == 20 and len({item.id for item in questions}) == 20
    assert sum(item.answerable for item in questions) == 15
    syllabus = json.loads((ROOT / "data/syllabus_jamb_bio.json").read_text(encoding="utf-8"))
    assert eval_set.goal == syllabus["goal"]["code"]
    topics = {topic["code"] for topic in syllabus["topics"]}
    catalog = yaml.safe_load((ROOT / "ingest/sources.yaml").read_text(encoding="utf-8"))
    sections = {section if isinstance(section, str) else section["title"]
                for source in catalog["sources"] for section in source["sections"]}
    for item in questions:
        if item.answerable:
            assert item.topic in topics and set(item.sections) <= sections, item.id
    assert {item.topic for item in questions if item.answerable} == topics  # every topic is exercised


def test_evaluate_reports_acceptance(sessions, clock, corpus, make_client):
    with sessions() as session:
        code = session.get(Goal, corpus.goal_id).code
    eval_set = qa_eval.EvalSet(goal=code, questions=[
        qa_eval.EvalQuestion(id="a1", question=LEAF_QUESTION, answerable=True, sections=["1.1 Section"]),
        qa_eval.EvalQuestion(id="o1", question="Who won the football match yesterday?", answerable=False),
    ])
    service = qa_service(make_client(FakeLLM(chat(answer_json("C1")), chat("yes"))), clock)
    with sessions() as session:
        report = qa_eval.evaluate(session, eval_set, embedder=EMBEDDER, qa=service, min_score=MIN_SCORE,
                                  now=clock.now(session))
        retrieval = qa_eval.evaluate(session, eval_set, embedder=EMBEDDER, qa=None, min_score=MIN_SCORE,
                                     now=clock.now(session))
        session.rollback()
    assert (report.answered_with_citation, report.out_of_scope_fallbacks, report.accepted_outside_retrieved) == (1, 1, 0)
    assert report.passed and "Answerable with valid citation: 1/1 (need 1)" in report.format()
    assert report.results[0].expected_rank == 1
    assert retrieval.passed and retrieval.results[0].answered is None
    assert "Threshold 0.300 keeps 1/1 answerable and stops 1/1 out-of-scope" in retrieval.format()
    with sessions() as session:  # rolled back: no synthetic learner or logs remain
        assert session.scalar(select(func.count()).select_from(Learner).where(Learner.first_name == "Evaluator")) == 0

    failing = qa_eval.EvalReport([qa_eval.QuestionResult(eval_set.questions[1], report.results[1].retrieved,
                                                         answered=True, cited_ids=[uuid4()])], MIN_SCORE, False)
    assert failing.accepted_outside_retrieved == 1 and not failing.passed


def test_eval_set_requires_sections_for_answerable_questions():
    with pytest.raises(ValueError, match="must list the sections"):
        qa_eval.EvalQuestion(id="x", question="?", answerable=True)


# Application wiring


def test_app_exposes_qa_service_without_loading_the_model(database_url, engine):
    settings = Settings(_env_file=None, database_url=database_url, hash_salt=SALT)
    with TestClient(create_app(settings)) as client:
        service = client.app.state.qa
        assert isinstance(service, QAService) and isinstance(service.embedder, LazyEmbedder)
        assert service.embedder._embedder is None and service.min_score == settings.retrieval_min_score


def test_lazy_embedder_builds_once():
    built = []
    lazy = LazyEmbedder(lambda: built.append(1) or FakeEmbedder())
    assert built == []
    lazy.embed_queries(["a"]), lazy.embed_passages(["b"]), lazy.count_tokens("c d")
    assert built == [1]
