import copy
import json
import threading
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.core import grading, lessons
from app.core.conversation import Conversation
from app.core.i18n import t
from app.core.lessons import LessonService, Rejection
from app.core.privacy import delete_learner
from app.core.qa import QAService
from app.llm import prompts
from app.models import (
    Answer, Chunk, Circle, CircleMember, FeedEvent, FeedType, Goal, Language, Learner, Lesson, PregeneratedLesson,
    Question, Source, SyllabusTopic,
)
from tests.fakes import FakeEmbedder, FakeLLM, chat
from tests.test_onboarding import SALT, User

EMBEDDER = FakeEmbedder()
LEAF_TEXTS = [
    "Leaves use chlorophyll to capture light energy for photosynthesis.",
    "Photosynthesis turns carbon dioxide and water into glucose and oxygen in leaves.",
    "Chloroplasts in leaf cells hold the chlorophyll that absorbs light.",
    "Stomata on leaves let carbon dioxide in and oxygen out during photosynthesis.",
    "The light reactions of photosynthesis split water and release oxygen.",
    "The Calvin cycle uses carbon dioxide to build sugar in the chloroplast.",
    "Plants store the glucose from photosynthesis as starch in leaves.",
]
KIDNEY_TEXTS = ["The kidney filters blood in each nephron.", "Urea leaves the body in urine stored in the bladder."]


@dataclass
class Library:
    goal_id: UUID
    topics: list[UUID]
    leaf_chunks: list[UUID]
    kidney_chunks: list[UUID]


@pytest.fixture
def library(sessions):
    """A goal with two ordered topics: photosynthesis (seven chunks) then excretion (two)."""
    with sessions.begin() as session:
        goal = Goal(code=f"test_{uuid4().hex}", title="Synthetic Biology", syllabus_ref="synthetic.json")
        source = Source(title="Synthetic Book", publisher="Test Press", license="CC0", url="https://example.org",
                        file_path="book.pdf")
        session.add_all([goal, source])
        session.flush()
        leaf = SyllabusTopic(goal_id=goal.id, order_index=1, title="Photosynthesis",
                             objectives="How leaves use chlorophyll, light, carbon dioxide and water to make glucose")
        kidney = SyllabusTopic(goal_id=goal.id, order_index=2, title="Excretion",
                               objectives="How the kidney filters blood and makes urine")
        session.add_all([leaf, kidney])
        session.flush()
        chunks = {}
        for topic, texts in ((leaf, LEAF_TEXTS), (kidney, KIDNEY_TEXTS)):
            chunks[topic.id] = [Chunk(source_id=source.id, section=f"{topic.order_index}.{index} Section",
                                      page=100 * topic.order_index + index, text=text,
                                      embedding=EMBEDDER.embed_passages([text])[0], syllabus_topic_id=topic.id)
                                for index, text in enumerate(texts, start=1)]
            session.add_all(chunks[topic.id])
        session.flush()
        return Library(goal.id, [leaf.id, kidney.id], [c.id for c in chunks[leaf.id]],
                       [c.id for c in chunks[kidney.id]])


def question(stem: str, correct: str, cite: str = "C1", follow_up: bool = True) -> dict:
    item = {"stem": stem, "options": {"A": "Glucose", "B": "Chlorophyll", "C": "Urea", "D": "Salt"},
            "correct_option": correct, "explanation": f"The chunks say so about {stem.lower()}",
            "cited_chunk_ids": [cite]}
    if follow_up:
        item["follow_up"] = question(f"Easier: {stem}", correct, cite, follow_up=False)
    return item


def lesson_dict(**overrides) -> dict:
    data = {"lesson": "Leaves capture light with chlorophyll and make glucose.",
            "example": "A leaf in sunlight makes glucose from carbon dioxide and water.",
            "cited_chunk_ids": ["C1", "C2"],
            "questions": [question("Which pigment captures light?", "B"),
                          question("What sugar do leaves make?", "A", cite="C2")]}
    return data | overrides


def lesson_reply(**overrides) -> str:
    return json.dumps(lesson_dict(**overrides))


FAITHFUL = json.dumps({"faithful": True, "problems": []})
UNFAITHFUL = json.dumps({"faithful": False, "problems": ["[Question 1] still in English"]})
PARTS = 5  # the lesson with its example, two questions and their two follow-ups, each claim-checked


def generated(reply: str | None = None) -> list:
    """A lesson reply followed by a passing claim check for every part."""
    return [chat(reply or lesson_reply()), *[chat("yes")] * PARTS]


def english_parts() -> dict[str, str]:
    """lesson_dict() as the translator receives it: part name -> English text."""
    return lessons.flatten(lessons.LessonText.model_validate(lesson_dict()))


def translation_reply(prefix: str = "YO", **overrides) -> str:
    """A well-formed translation: every part prefixed. Override a part by name; None removes it."""
    parts = {name: f"{prefix} {text}" if prefix else text for name, text in english_parts().items()}
    for name, value in overrides.items():
        if value is None:
            parts.pop(name)
        else:
            parts[name] = value
    return json.dumps(parts)


@pytest.fixture
def make_conversation(sessions, clock, make_client):
    def make(*responses, fake: FakeLLM | None = None, **client_options) -> Conversation:
        client = make_client(fake or FakeLLM(*responses), **client_options)
        return Conversation(sessions, clock, QAService(EMBEDDER, client, clock, min_score=0.3),
                            LessonService(EMBEDDER, client, clock), salt=SALT)
    return make


def onboarded(conversation: Conversation, library: Library, **options) -> User:
    user = User(conversation)
    user.onboard(library.goal_id, **options)
    return user


def learner_of(sessions, user: User) -> Learner:
    with sessions() as session:
        return session.scalar(select(Learner).where(Learner.channel_user_hash == user.hash))


def latest_lesson(sessions, library: Library) -> Lesson:
    with sessions() as session:
        return session.scalar(select(Lesson).join(Circle, Circle.id == Lesson.circle_id)
                              .where(Circle.goal_id == library.goal_id).order_by(Lesson.day_index.desc()).limit(1))


def questions_of(sessions, lesson: Lesson) -> list[Question]:
    with sessions() as session:
        return lessons.lesson_questions(session, lesson.id)


def payloads(reply) -> list[str]:
    return [button.payload for button in reply.buttons]


# Generation


def test_lesson_is_generated_from_six_topic_chunks_and_cited(sessions, library, make_conversation):
    fake = FakeLLM(*generated())
    user = onboarded(make_conversation(fake=fake), library, name="Adaeze")
    (reply,) = user.send("/lesson")

    generation, *checks = fake.requests
    prompt = generation["messages"][1]["content"]
    assert generation["messages"][0]["content"] == prompts.LESSON_SYSTEM
    assert generation["response_format"] == {"type": "json_object"}
    assert "Topic: Photosynthesis" in prompt
    assert prompt.count("[C") == lessons.LESSON_CHUNKS == 6  # top six of the topic's seven chunks
    assert all(text not in prompt for text in KIDNEY_TEXTS)  # the other topic's chunks never appear
    assert len(checks) == PARTS and all(c["messages"][0]["content"] == prompts.LESSON_CHECK_SYSTEM for c in checks)
    assert "Worked example: A leaf in sunlight" in checks[0]["messages"][1]["content"]
    assert "Marked correct answer: B" in checks[1]["messages"][1]["content"]
    assert "Adaeze" not in json.dumps(fake.requests) and user.chat_id not in json.dumps(fake.requests)

    assert reply.text.startswith("📘 Day 1: Photosynthesis\nLeaves capture light with chlorophyll")
    assert "Example: A leaf in sunlight" in reply.text
    assert "Source: Synthetic Book, 1." in reply.text
    lesson = latest_lesson(sessions, library)
    assert payloads(reply)[-1] == f"{lessons.START_QUESTIONS}{lesson.id}"
    assert all(payload.startswith("qa:src:") for payload in payloads(reply)[:-1])
    assert lesson.fallback_reason is None and lesson.day_index == 1 and lesson.topic_id == library.topics[0]
    assert len(lesson.cited_chunk_ids) == 2 and set(lesson.cited_chunk_ids) <= set(library.leaf_chunks)
    assert len(reply.text.split()) < 200

    questions = questions_of(sessions, lesson)
    assert [(q.position, q.parent_question_id is None, q.difficulty) for q in questions] == [
        (1, True, 2), (1, False, 1), (2, True, 2), (2, False, 1)]
    assert questions[1].parent_question_id == questions[0].id
    assert questions[0].correct_option == "B" and list(questions[0].options) == ["A", "B", "C", "D"]
    assert all(set(q.cited_chunk_ids) <= set(library.leaf_chunks) for q in questions)


def test_lesson_is_shared_for_the_day_then_moves_to_the_next_topic(sessions, clock, library, make_conversation):
    fake = FakeLLM(*generated())
    conversation = make_conversation(fake=fake)
    first, second = onboarded(conversation, library), onboarded(conversation, library, name="Bola")
    first.send("/lesson")
    (again,) = second.send("/lesson")
    assert len(fake.requests) == 1 + PARTS  # the second learner gets the same lesson without new LLM calls
    assert again.text.startswith("📘 Day 1: Photosynthesis")

    clock.advance(timedelta(days=1))
    fake.responses += generated(lesson_reply(cited_chunk_ids=["C1"]))
    (next_day,) = first.send("/lesson")
    assert next_day.text.startswith("📘 Day 2: Excretion")
    assert "Topic: Excretion" in fake.requests[1 + PARTS]["messages"][1]["content"]
    assert set(latest_lesson(sessions, library).cited_chunk_ids) <= set(library.kidney_chunks)

    clock.advance(timedelta(days=1))
    fake.responses += generated()
    (wrapped,) = first.send("/lesson")
    assert wrapped.text.startswith("📘 Day 3: Photosynthesis")  # after the last topic, revision starts over


def invalid(**overrides) -> str:
    return lesson_reply(**overrides)


def with_question(index: int, **changes) -> list[dict]:
    items = copy.deepcopy(lesson_dict()["questions"])
    items[index] |= changes
    return items


@pytest.mark.parametrize("bad,reason", [
    ("Here is your lesson!", Rejection.INVALID_JSON),
    (json.dumps({"lesson": "Too little"}), Rejection.SCHEMA_MISMATCH),
    (invalid(lesson=" ".join(["word"] * 151)), Rejection.TOO_LONG),
    (invalid(example=" ".join(["word"] * 101)), Rejection.TOO_LONG),
    (invalid(questions=lesson_dict()["questions"][:1]), Rejection.SCHEMA_MISMATCH),
    (invalid(questions=lesson_dict()["questions"] * 2), Rejection.SCHEMA_MISMATCH),
    (invalid(questions=with_question(0, options={"A": "x", "B": "y", "C": "z"})), Rejection.INVALID_OPTIONS),
    (invalid(questions=with_question(0, options={"A": "x", "B": "x", "C": "y", "D": "z"})), Rejection.INVALID_OPTIONS),
    (invalid(questions=with_question(0, correct_option="E")), Rejection.INVALID_OPTIONS),
    (invalid(questions=with_question(1, cited_chunk_ids=[])), Rejection.NO_CITATIONS),
    (invalid(cited_chunk_ids=[]), Rejection.NO_CITATIONS),
    (invalid(cited_chunk_ids=["C7"]), Rejection.CITATION_OUTSIDE_RETRIEVED),  # only C1 to C6 were shown
    (invalid(cited_chunk_ids=[str(uuid4())]), Rejection.CITATION_OUTSIDE_RETRIEVED),
    (invalid(questions=with_question(0, follow_up=lesson_dict()["questions"][0] | {"follow_up": None})),
     Rejection.SCHEMA_MISMATCH),  # the follow-up repeats the question
    (invalid(questions=with_question(0, explanation="[C1]")), Rejection.SCHEMA_MISMATCH),
])
def test_invalid_lessons_are_retried_once_with_the_reason(sessions, library, make_client, clock, bad, reason):
    fake = FakeLLM(chat(bad), *generated())
    service = LessonService(EMBEDDER, make_client(fake), clock)
    with sessions() as session:
        shown = lessons.load_chunks(session, library.leaf_chunks[:lessons.LESSON_CHUNKS])
        with pytest.raises(lessons.Rejected) as caught:
            lessons.validate_lesson(lessons._json(bad), shown)
        assert caught.value.reason == reason
        draft = service.generate(session, session.get(SyllabusTopic, library.topics[0]))
    assert draft.questions[0].correct_option == "B"
    first, retry = fake.requests[:2]
    assert retry["messages"][:2] == first["messages"]
    assert retry["messages"][2] == {"role": "assistant", "content": bad}
    assert retry["messages"][3]["content"].startswith("That reply was rejected: ")


@pytest.mark.parametrize("verdict,feedback", [("no", "the chunks question 1 cites do not support it"),
                                              ("Yes, but the example adds a fact", "could not confirm question 1")])
def test_claim_check_rejection_is_retried(sessions, library, make_client, clock, verdict, feedback):
    # The lesson part passes, question 1 fails, so checking stops and the lesson is regenerated.
    fake = FakeLLM(chat(lesson_reply()), chat("yes"), chat(verdict), *generated(lesson_reply(cited_chunk_ids=["C2"])))
    service = LessonService(EMBEDDER, make_client(fake), clock)
    with sessions() as session:
        draft = service.generate(session, session.get(SyllabusTopic, library.topics[0]))
    assert len(fake.requests) == 3 + 1 + PARTS
    assert feedback in fake.requests[3]["messages"][3]["content"]
    assert len(draft.cited_chunk_ids) == 1


def test_each_part_is_checked_against_only_its_own_citations(sessions, library, make_client, clock):
    """A question cannot borrow support from a chunk that only the lesson cites."""
    with sessions() as session:
        topic = session.get(SyllabusTopic, library.topics[0])
        shown = lessons.retrieve(session, EMBEDDER, library.goal_id, lessons.topic_query(topic),
                                 limit=lessons.LESSON_CHUNKS, topic_id=topic.id)
    chlorophyll = next(i for i, chunk in enumerate(shown) if "chlorophyll" in chunk.text)
    sugar_only = next(i for i, chunk in enumerate(shown) if "chlorophyll" not in chunk.text
                      and "glucose" in chunk.text)
    items = with_question(0, cited_chunk_ids=[f"C{sugar_only + 1}"])  # chlorophyll question, sugar-only citation
    reply = lesson_reply(cited_chunk_ids=[f"C{chlorophyll + 1}"], questions=items)
    fake = FakeLLM(chat(reply), chat("yes"), chat("no"), *generated())
    service = LessonService(EMBEDDER, make_client(fake), clock)
    with sessions() as session:
        service.generate(session, session.get(SyllabusTopic, library.topics[0]))
    lesson_check, question_check = (request["messages"][1]["content"] for request in fake.requests[1:3])
    assert shown[chlorophyll].text in lesson_check
    assert "Which pigment captures light?" in question_check
    assert shown[sugar_only].text in question_check and shown[chlorophyll].text not in question_check
    assert question_check.count("[C") == 1
    assert "question 1 cites do not support it" in fake.requests[3]["messages"][3]["content"]


def test_validation_resolves_labels_to_the_chunks_shown(sessions, library):
    with sessions() as session:
        shown = lessons.load_chunks(session, library.leaf_chunks[:6])
    draft = lessons.validate_lesson(lesson_dict(cited_chunk_ids=["C3", f" [{shown[0].id}] ", "c3"]), shown)
    assert draft.cited_chunk_ids == [shown[2].id, shown[0].id]
    assert draft.questions[1].follow_up.cited_chunk_ids == [shown[1].id]
    padded = lesson_dict(questions=with_question(0, correct_option=" b. ", stem="Which pigment? (C1)"))
    assert lessons.validate_lesson(padded, shown).questions[0].correct_option == "B"
    assert lessons.validate_lesson(padded, shown).questions[0].stem == "Which pigment?"


def pregenerate(sessions, clock, make_client, library, topic_index=0, languages=(), *responses):
    service = LessonService(EMBEDDER, make_client(FakeLLM(*responses)), clock)
    with sessions.begin() as session:
        return service.pregenerate(session, session.get(SyllabusTopic, library.topics[topic_index]), languages,
                                   clock.now(session))


def test_failed_generation_falls_back_to_the_pregenerated_lesson(sessions, clock, library, make_client,
                                                                 make_conversation):
    result = pregenerate(sessions, clock, make_client, library, 0, [Language.YO],
                         *generated(), chat(translation_reply()), chat(FAITHFUL))
    assert result.draft is not None and result.translation_failures == {}

    # Live generation now fails twice: once invalid, once on retry.
    fake = FakeLLM(chat("not json"), chat(lesson_reply(lesson="")))
    user = onboarded(make_conversation(fake=fake), library, language=Language.YO)
    replies = user.send("/lesson")
    assert len(fake.requests) == 2  # the stored translation is reused, so no translation call
    lesson = latest_lesson(sessions, library)
    assert lesson.fallback_reason == Rejection.SCHEMA_MISMATCH.value
    assert lesson.body["yo"]["lesson"].startswith("YO Leaves")
    # Lesson content in Yoruba; the header stays English until the Yoruba UI strings are reviewed.
    assert replies[-1].text.startswith("📘 Day 1: Photosynthesis\nYO Leaves")


def test_without_a_pregenerated_lesson_the_learner_is_told_to_try_later(sessions, library, make_conversation):
    fake = FakeLLM(chat("not json"), chat("still not json"))
    user = onboarded(make_conversation(fake=fake), library)
    assert [reply.text for reply in user.send("/lesson")] == [t("lesson.unavailable")]
    assert latest_lesson(sessions, library) is None


def test_offline_llm_uses_the_pregenerated_lesson(sessions, clock, library, make_client, make_conversation):
    pregenerate(sessions, clock, make_client, library, 0, (), *generated())
    user = onboarded(make_conversation(enabled=False), library)
    (reply,) = user.send("/lesson")
    assert reply.text.startswith("📘 Day 1: Photosynthesis")
    assert latest_lesson(sessions, library).fallback_reason == Rejection.LLM_UNAVAILABLE.value


def test_rehearsed_lesson_needs_no_live_calls(sessions, clock, library, make_client, make_conversation):
    """With LLM_CACHE, a pre-generated topic's live generation is served from the cache."""
    fake = FakeLLM(*generated())
    service = LessonService(EMBEDDER, make_client(fake, cache=True), clock)
    with sessions.begin() as session:
        service.pregenerate(session, session.get(SyllabusTopic, library.topics[0]), (), clock.now(session))
    user = onboarded(make_conversation(fake=FakeLLM(), cache=True, enabled=False), library)
    user.send("/lesson")
    assert latest_lesson(sessions, library).fallback_reason is None  # generated, from the cache


# Translation


def test_lesson_is_translated_with_citations_kept_in_english(sessions, library, make_conversation):
    fake = FakeLLM(*generated(), chat(translation_reply()), chat(FAITHFUL))
    user = onboarded(make_conversation(fake=fake), library, language=Language.PCM)
    user.send("/lesson")
    translate, check = fake.requests[1 + PARTS:]
    assert translate["messages"][0]["content"] == prompts.TRANSLATE_SYSTEM.format(language="Nigerian Pidgin")
    assert "Biology terms in English in brackets" in translate["messages"][0]["content"]
    sent = json.loads(translate["messages"][1]["content"])
    assert sent == english_parts() and sent["Question 1, option B"] == "Chlorophyll"
    assert "correct_option" not in json.dumps(sent) and "cited_chunk_ids" not in json.dumps(sent)
    assert check["messages"][0]["content"] == prompts.TRANSLATION_CHECK_SYSTEM.format(language="Nigerian Pidgin")
    fidelity = check["messages"][1]["content"]
    assert fidelity.startswith("Target language: Nigerian Pidgin")
    assert ("[Lesson]\nEnglish: Leaves capture light with chlorophyll and make glucose.\n"
            "Nigerian Pidgin: YO Leaves capture light with chlorophyll and make glucose.") in fidelity
    assert "[Follow-up to question 2, option A]\nEnglish: Glucose\nNigerian Pidgin: YO Glucose" in fidelity
    assert "Source:" not in fidelity and "[C1]" not in fidelity  # citations are not translated or re-checked

    lesson = latest_lesson(sessions, library)
    questions = questions_of(sessions, lesson)
    assert lesson.body["pcm"] == {"lesson": "YO Leaves capture light with chlorophyll and make glucose.",
                                  "example": "YO A leaf in sunlight makes glucose from carbon dioxide and water."}
    assert questions[0].translations["pcm"]["options"]["B"] == "YO Chlorophyll"
    assert questions[1].translations["pcm"]["stem"].startswith("YO Easier")
    assert questions[0].stem == "Which pigment captures light?"  # English kept alongside
    with sessions() as session:
        replies = lessons.lesson_replies(session, lesson, learner_of(sessions, user))
    assert "YO Leaves" in replies[-1].text and "Source: Synthetic Book, 1." in replies[-1].text  # not translated

    (question_message,) = user.tap(f"{lessons.START_QUESTIONS}{lesson.id}")
    assert "YO Which pigment" in question_message.text and "B. YO Chlorophyll" in question_message.text


@pytest.mark.parametrize("bad", [
    translation_reply(**{"Question 1": None}),  # a part is missing
    translation_reply(**{"Question 1, option D": None}),
    translation_reply(Extra="YO extra"),  # a part was invented
    translation_reply(Lesson=""),
    translation_reply(Lesson=7),
    "not json",
    translation_reply(Lesson="YO " * 500),
    translation_reply(**{"Question 2, option B": "YO Glucose"}),  # two options now say the same
])
def test_rejected_translation_falls_back_to_english_with_a_note(sessions, library, make_conversation, bad):
    fake = FakeLLM(*generated(), chat(bad), chat(bad))
    user = onboarded(make_conversation(fake=fake), library, language=Language.YO)
    note, reply = user.send("/lesson")
    assert note.text == t("lesson.english_only", Language.YO)
    assert reply.text.split("\n")[1].startswith("Leaves capture")
    assert "yo" not in latest_lesson(sessions, library).body
    assert len(fake.requests) == 1 + PARTS + 2  # one retry, then English


def test_unfaithful_translation_is_rejected(sessions, library, make_conversation):
    fake = FakeLLM(*generated(), chat(translation_reply()), chat(UNFAITHFUL),
                   chat(translation_reply("PCM")), chat(UNFAITHFUL))
    user = onboarded(make_conversation(fake=fake), library, language=Language.YO)
    note, _ = user.send("/lesson")
    assert note.text == t("lesson.english_only")
    retry = fake.requests[1 + PARTS + 2]["messages"][-1]["content"]
    assert "not in Yoruba or that change the meaning of the English ([Question 1] still in English)" in retry
    assert "Target language: Yoruba" in fake.requests[1 + PARTS + 1]["messages"][1]["content"]


@pytest.mark.parametrize("bad", [translation_reply(""), translation_reply("", Lesson="YO only the lesson changed")])
def test_english_returned_as_a_translation_is_rejected_without_a_check(sessions, library, make_client, clock, bad):
    fake = FakeLLM(chat(bad), chat(bad))
    service = LessonService(EMBEDDER, make_client(fake), clock)
    draft = lessons.validate_lesson(lesson_dict(), _shown_chunks(sessions, library))
    with pytest.raises(lessons.Rejected) as caught:
        service.translate(draft, Language.YO)
    assert caught.value.reason == Rejection.UNTRANSLATED
    assert len(fake.requests) == 2  # no fidelity check is spent on an untranslated reply
    assert "left in English" in fake.requests[1]["messages"][-1]["content"]


def test_untranslated_questions_and_explanations_are_named_for_the_retry(sessions, library, make_client, clock):
    english = english_parts()
    partial = translation_reply(**{"Follow-up to question 2": english["Follow-up to question 2"],
                                   "Question 1, explanation": english["Question 1, explanation"]})
    fake = FakeLLM(chat(partial), chat(translation_reply()), chat(FAITHFUL))
    service = LessonService(EMBEDDER, make_client(fake), clock)
    draft = lessons.validate_lesson(lesson_dict(), _shown_chunks(sessions, library))
    assert service.translate(draft, Language.YO).questions[1].follow_up.stem.startswith("YO ")
    assert ("left in English: Question 1, explanation, Follow-up to question 2."
            in fake.requests[1]["messages"][-1]["content"])


def test_options_may_match_the_english_and_go_to_the_fidelity_check(sessions, library, make_client, clock):
    """An option may rightly be just a Biology term, so the reviewer, not the code, judges it."""
    fake = FakeLLM(chat(translation_reply(**{"Question 1, option B": "Chlorophyll"})), chat(FAITHFUL))
    service = LessonService(EMBEDDER, make_client(fake), clock)
    draft = lessons.validate_lesson(lesson_dict(), _shown_chunks(sessions, library))
    assert service.translate(draft, Language.YO).questions[0].options["B"] == "Chlorophyll"
    check = fake.requests[1]
    assert check["response_format"] == {"type": "json_object"}
    assert "[Question 1, option B]\nEnglish: Chlorophyll\nYoruba: Chlorophyll" in check["messages"][1]["content"]


@pytest.mark.parametrize("review", ["yes", json.dumps({"faithful": "yes"}), json.dumps({"problems": []})])
def test_malformed_translation_review_is_a_check_error(sessions, library, make_client, clock, review):
    fake = FakeLLM(chat(translation_reply()), chat(review), chat(translation_reply("PCM")), chat(review))
    service = LessonService(EMBEDDER, make_client(fake), clock)
    draft = lessons.validate_lesson(lesson_dict(), _shown_chunks(sessions, library))
    with pytest.raises(lessons.Rejected) as caught:
        service.translate(draft, Language.YO)
    assert caught.value.reason == Rejection.CLAIM_CHECK_ERROR


def _shown_chunks(sessions, library):
    with sessions() as session:
        return lessons.load_chunks(session, library.leaf_chunks[:lessons.LESSON_CHUNKS])


def test_existing_lesson_is_translated_for_a_new_language(sessions, library, make_conversation):
    fake = FakeLLM(*generated())
    conversation = make_conversation(fake=fake)
    english, yoruba = onboarded(conversation, library), onboarded(conversation, library, language=Language.YO)
    english.send("/lesson")
    fake.responses += [chat(translation_reply()), chat(FAITHFUL)]
    (reply,) = yoruba.send("/lesson")
    lesson = latest_lesson(sessions, library)
    assert set(lesson.body) == {"en", "yo"} and len(fake.requests) == 1 + PARTS + 2
    assert all("yo" in question.translations for question in questions_of(sessions, lesson))
    assert yoruba.send("/lesson")[0].text == reply.text and len(fake.requests) == 1 + PARTS + 2  # stored


# Answering


def start_questions(sessions, library, user: User):
    lesson = latest_lesson(sessions, library)
    (first,) = user.tap(f"{lessons.START_QUESTIONS}{lesson.id}")
    return lesson, questions_of(sessions, lesson), first


def test_questions_arrive_one_at_a_time_with_private_follow_up(sessions, clock, library, make_conversation):
    user = onboarded(make_conversation(*generated()), library)
    user.send("/lesson")
    lesson, (q1, q1_easy, q2, q2_easy), first = start_questions(sessions, library, user)
    assert first.text == "Question 1 of 2\nWhich pigment captures light?\n\nA. Glucose\nB. Chlorophyll\nC. Urea\nD. Salt"
    assert payloads(first) == [f"{lessons.ANSWER}{q1.id}:{key}" for key in "ABCD"]
    assert all(len(payload.encode()) <= 64 for payload in payloads(first))

    correct, second = user.tap(f"{lessons.ANSWER}{q1.id}:B")
    assert correct.text == t("question.correct")
    assert second.text.startswith("Question 2 of 2\nWhat sugar do leaves make?")

    wrong, easier = user.tap(f"{lessons.ANSWER}{q2.id}:C")
    assert wrong.text.startswith("❌ Not quite. The answer is A: Glucose\nThe chunks say so")
    assert "Source: Synthetic Book, 1." in wrong.text and payloads(wrong)[0].startswith("qa:src:")
    assert easier.text.startswith(t("question.easier") + "\nEasier: What sugar")

    with sessions() as session:
        member = session.get(CircleMember, (lesson.circle_id, learner_of(sessions, user).id))
        assert member.last_checkin_at is not None and member.streak_days == 1
        events = session.scalars(select(FeedEvent).where(FeedEvent.type == FeedType.ANSWERED,
                                                         FeedEvent.circle_id == lesson.circle_id)).all()
        assert len(events) == 1  # both main questions answered
        assert events[0].payload == {"lesson_id": str(lesson.id), "day_index": 1}  # never a score
        assert events[0].actor_learner_id == learner_of(sessions, user).id

    right_again, done = user.tap(f"{lessons.ANSWER}{q2_easy.id}:A")
    assert right_again.text == t("question.correct") and done.text == t("question.done")
    assert user.tap(f"{lessons.START_QUESTIONS}{lesson.id}")[0].text == t("question.all_answered")
    with sessions() as session:
        answers = session.scalars(select(Answer).where(Answer.learner_id == learner_of(sessions, user).id)).all()
        assert sorted((a.question_id, a.chosen_option, a.is_correct) for a in answers) == sorted(
            [(q1.id, "B", True), (q2.id, "C", False), (q2_easy.id, "A", True)])
        assert session.scalar(select(func.count()).select_from(FeedEvent)
                              .where(FeedEvent.circle_id == lesson.circle_id,
                                     FeedEvent.type == FeedType.ANSWERED)) == 1


def test_a_wrong_first_answer_gets_its_follow_up_before_question_two(sessions, library, make_conversation):
    user = onboarded(make_conversation(*generated()), library)
    user.send("/lesson")
    _, (q1, q1_easy, q2, _), _ = start_questions(sessions, library, user)
    _, easier = user.tap(f"{lessons.ANSWER}{q1.id}:A")
    assert payloads(easier)[0] == f"{lessons.ANSWER}{q1_easy.id}:A"
    wrong_again, second = user.tap(f"{lessons.ANSWER}{q1_easy.id}:D")
    assert wrong_again.text.startswith("❌") and second.text.startswith("Question 2 of 2")
    assert len(user.tap(f"{lessons.ANSWER}{q2.id}:A")) == 2  # correct, then done: no follow-up needed


def test_repeated_and_forged_taps_change_nothing(sessions, library, make_conversation):
    conversation = make_conversation(*generated())
    user = onboarded(conversation, library)
    user.send("/lesson")
    lesson, (q1, q1_easy, q2, _), _ = start_questions(sessions, library, user)
    user.tap(f"{lessons.ANSWER}{q1.id}:B")
    repeat = user.tap(f"{lessons.ANSWER}{q1.id}:A")
    assert repeat[0].text == t("question.already_answered") and repeat[1].text.startswith("Question 2 of 2")
    # A follow-up is only open after a wrong answer, and options must exist.
    assert user.tap(f"{lessons.ANSWER}{q1_easy.id}:B")[0].text == t("button.expired")
    for payload in (f"{lessons.ANSWER}{q2.id}:E", f"{lessons.ANSWER}{uuid4()}:A", f"{lessons.ANSWER}junk",
                    f"{lessons.START_QUESTIONS}{uuid4()}", f"{lessons.START_QUESTIONS}nope", "ls:other"):
        assert user.tap(payload)[0].text == t("button.expired")
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Answer)
                              .where(Answer.learner_id == learner_of(sessions, user).id)) == 1
        assert session.get(Answer, session.scalar(select(Answer.id).where(Answer.question_id == q1.id))).is_correct


def test_only_circle_members_can_answer(sessions, library, make_conversation, clock):
    conversation = make_conversation(*generated())
    user = onboarded(conversation, library)
    user.send("/lesson")
    _, (q1, *_), _ = start_questions(sessions, library, user)
    stranger = onboarded(conversation, library, name="Bola")
    with sessions.begin() as session:  # a learner who left the circle
        learner = session.get(Learner, learner_of(sessions, stranger).id)
        session.query(CircleMember).filter(CircleMember.learner_id == learner.id).delete()
    assert stranger.tap(f"{lessons.ANSWER}{q1.id}:B")[0].text == t("button.expired")
    assert stranger.send("/lesson")[0].text == t("lesson.not_in_circle")


def test_check_in_streak_counts_consecutive_virtual_days(sessions, clock, library):
    with sessions.begin() as session:
        goal = session.get(Goal, library.goal_id)
        circle = Circle(goal_id=goal.id, name="Streaks", start_date=clock.now(session).date(),
                        created_at=clock.now(session))
        learner = Learner(channel="web", channel_user_hash=uuid4().hex, first_name="Ada", goal_id=goal.id,
                          created_at=clock.now(session))
        session.add_all([circle, learner])
        session.flush()
        session.add(CircleMember(circle_id=circle.id, learner_id=learner.id, joined_at=clock.now(session)))
    start = clock.now()
    for offset, expected in ((timedelta(0), 1), (timedelta(hours=1), 1), (timedelta(days=1), 2),
                             (timedelta(days=3), 1), (timedelta(days=4), 2)):
        moment = start.replace(hour=0) + offset
        with sessions.begin() as session:
            grading.check_in(session, circle.id, learner.id, moment)
        with sessions() as session:
            member = session.get(CircleMember, (circle.id, learner.id))
            assert member.streak_days == expected and member.last_checkin_at is not None


def test_deleting_a_learner_removes_their_answers_and_feed_events(sessions, library, make_conversation):
    user = onboarded(make_conversation(*generated()), library)
    user.send("/lesson")
    _, (q1, _, q2, _), _ = start_questions(sessions, library, user)
    user.tap(f"{lessons.ANSWER}{q1.id}:B")
    user.tap(f"{lessons.ANSWER}{q2.id}:A")
    learner_id = learner_of(sessions, user).id
    with sessions.begin() as session:
        assert delete_learner(session, learner_id, confirmed=True)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Answer).where(Answer.learner_id == learner_id)) == 0
        assert session.scalar(select(func.count()).select_from(FeedEvent)
                              .where(FeedEvent.actor_learner_id == learner_id)) == 0
        assert session.get(Lesson, latest_lesson(sessions, library).id) is not None  # circle content stays


def test_post_translates_for_every_member_and_skips_simulated_ones(sessions, clock, library, make_client,
                                                                    make_conversation):
    conversation = make_conversation()
    real = onboarded(conversation, library)
    with sessions.begin() as session:
        circle = session.scalar(select(Circle).where(Circle.goal_id == library.goal_id))
        persona = Learner(channel="web", channel_user_hash=uuid4().hex, first_name="Halima", language=Language.PCM,
                          goal_id=library.goal_id, is_simulated=True, created_at=clock.now(session))
        session.add(persona)
        session.flush()
        session.add(CircleMember(circle_id=circle.id, learner_id=persona.id, joined_at=clock.now(session)))
    fake = FakeLLM(*generated(), chat(translation_reply("PCM")), chat(FAITHFUL))
    service = LessonService(EMBEDDER, make_client(fake), clock)
    with sessions.begin() as session:
        lesson, deliveries = service.post(session, session.get(Circle, circle.id), clock.now(session))
        assert set(lesson.body) == {"en", "pcm"}
        assert [learner.id for learner, _ in deliveries] == [learner_of(sessions, real).id]
        assert deliveries[0][1][0].text.startswith("📘 Day 1")


@pytest.mark.postgres
def test_concurrent_lesson_requests_create_one_lesson(database_url, sessions, clock, library, make_client):
    if not database_url.startswith("postgresql"):
        pytest.skip("row locks need PostgreSQL")
    with sessions.begin() as session:
        goal = session.get(Goal, library.goal_id)
        circle = Circle(goal_id=goal.id, name="Race", start_date=clock.now(session).date(),
                        created_at=clock.now(session))
        session.add(circle)
    fake = FakeLLM(*generated())
    service = LessonService(EMBEDDER, make_client(fake), clock)
    barrier, results, errors = threading.Barrier(2), [], []

    def request():
        try:
            barrier.wait()
            with sessions.begin() as session:
                results.append(service.todays_lesson(session, session.get(Circle, circle.id), clock.now(session)).id)
        except Exception as error:  # surfaced below
            errors.append(error)

    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and len(set(results)) == 1 and len(fake.requests) == 1 + PARTS
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Lesson).where(Lesson.circle_id == circle.id)) == 1


# Pre-generation


def test_pregenerate_stores_validated_lessons_and_translations(sessions, clock, library, make_client):
    result = pregenerate(sessions, clock, make_client, library, 0, [Language.YO, Language.PCM],
                         *generated(), chat(translation_reply("PCM")), chat(FAITHFUL),
                         chat("garbage"), chat("garbage"))
    assert result.translation_failures == {Language.YO: Rejection.INVALID_JSON}
    with sessions() as session:
        stored = session.get(PregeneratedLesson, library.topics[0])
        draft = lessons.LessonDraft.model_validate(stored.content)
    assert set(draft.translations) == {Language.PCM}
    assert set(stored.cited_chunk_ids) == set(draft.chunk_ids()) <= set(library.leaf_chunks)
    assert "FAILED: yo (invalid_json)" in lessons.format_result(result)

    failed = pregenerate(sessions, clock, make_client, library, 0, (), chat("bad"), chat("bad"))
    assert failed.draft is None and "FAILED (invalid_json" in lessons.format_result(failed)
    with sessions() as session:  # the earlier lesson is kept
        assert session.get(PregeneratedLesson, library.topics[0]).content == stored.content


def test_pregenerate_cli_requires_the_flag():
    with pytest.raises(SystemExit):
        lessons.parse_args([])
    args = lessons.parse_args(["--pregenerate", "--topics", "1", "2", "--languages", "yo"])
    assert args.topics == [1, 2] and args.languages == ["yo"]
