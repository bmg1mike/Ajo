"""Run the QA evaluation set (spec section 16) against the ingested corpus.

Usage: uv run python -m app.core.qa_eval [--retrieval-only] [--min-score 0.80] [--report path]

Each question runs through the real pipeline for a temporary synthetic learner inside
a transaction that is rolled back, so qa_log is left untouched. LLM responses are still
cached (LLM_CACHE=true), so repeated runs make no live calls.
"""
import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.embeddings import Embedder
from app.core.qa import QAService
from app.core.retrieval import RetrievedChunk, retrieve
from app.models import Channel, Goal, Language, Learner

MIN_ANSWERED = 13  # of 15 answerable
DEFAULT_EVAL_SET = Path("data/qa_eval_jamb_bio.json")


class EvalQuestion(BaseModel):
    id: str
    question: str
    answerable: bool
    topic: str | None = None
    sections: list[str] = []

    @model_validator(mode="after")
    def answerable_needs_sections(self) -> "EvalQuestion":
        if self.answerable and not self.sections:
            raise ValueError(f"answerable question {self.id} must list the sections that answer it")
        return self


class EvalSet(BaseModel):
    goal: str
    description: str = ""
    questions: list[EvalQuestion]


def load_eval_set(path: Path) -> EvalSet:
    return EvalSet.model_validate(json.loads(path.read_text(encoding="utf-8")))


@dataclass
class QuestionResult:
    item: EvalQuestion
    retrieved: list[RetrievedChunk]
    answered: bool | None = None  # None in retrieval-only mode
    fallback_reason: str | None = None
    cited_ids: list = field(default_factory=list)
    answer: str = ""

    @property
    def top_score(self) -> float:
        return self.retrieved[0].score if self.retrieved else 0.0

    @property
    def expected_rank(self) -> int | None:
        return next((rank for rank, chunk in enumerate(self.retrieved, start=1)
                     if chunk.section in self.item.sections), None)

    @property
    def citations_outside_retrieved(self) -> bool:
        return bool(set(self.cited_ids) - {chunk.id for chunk in self.retrieved})


@dataclass
class EvalReport:
    results: list[QuestionResult]
    min_score: float
    retrieval_only: bool

    def _of(self, answerable: bool) -> list[QuestionResult]:
        return [result for result in self.results if result.item.answerable is answerable]

    @property
    def answered_with_citation(self) -> int:
        return sum(1 for r in self._of(True) if r.answered and r.cited_ids and not r.citations_outside_retrieved)

    @property
    def out_of_scope_fallbacks(self) -> int:
        return sum(1 for r in self._of(False) if r.answered is False)

    @property
    def accepted_outside_retrieved(self) -> int:
        return sum(1 for r in self.results if r.answered and r.citations_outside_retrieved)

    @property
    def retrieval_hits(self) -> int:
        return sum(1 for r in self._of(True) if r.expected_rank is not None)

    @property
    def passed(self) -> bool:
        answerable, out_of_scope = self._of(True), self._of(False)
        if self.retrieval_only:
            return self.retrieval_hits == len(answerable)
        return (self.answered_with_citation >= min(MIN_ANSWERED, len(answerable))
                and self.out_of_scope_fallbacks == len(out_of_scope) and self.accepted_outside_retrieved == 0)

    def format(self) -> str:
        lines = ["# QA evaluation", "", f"Retrieval threshold: {self.min_score:.3f}", ""]
        for result in self.results:
            rank = result.expected_rank
            if result.answered is None:
                outcome = ""
            elif result.answered:
                labels = ", ".join(chunk.citation for chunk in result.retrieved if chunk.id in set(result.cited_ids))
                outcome = f" -> answered [{labels}]"
            else:
                outcome = f" -> fallback ({result.fallback_reason})"
            expected = f" expected@{rank}" if rank else (" expected: not retrieved" if result.item.answerable else "")
            lines.append(f"- {result.item.id} top {result.top_score:.3f}{expected}{outcome}: {result.item.question}")
            if result.answer:
                lines.append(f"    {result.answer}")
        answerable, out_of_scope = self._of(True), self._of(False)
        lines += ["", "## Retrieval",
                  f"Expected section in top {len(answerable[0].retrieved) if answerable and answerable[0].retrieved else 0}: "
                  f"{self.retrieval_hits}/{len(answerable)} answerable questions"]
        if answerable and out_of_scope:
            low = min(r.top_score for r in answerable)
            high = max(r.top_score for r in out_of_scope)
            gated = sum(1 for r in out_of_scope if r.top_score < self.min_score)
            lines += [f"Top score: answerable min {low:.3f}, out-of-scope max {high:.3f}",
                      f"Threshold {self.min_score:.3f} keeps {sum(1 for r in answerable if r.top_score >= self.min_score)}"
                      f"/{len(answerable)} answerable and stops {gated}/{len(out_of_scope)} out-of-scope before the LLM"]
        if not self.retrieval_only:
            lines += ["", "## Acceptance (spec section 16)",
                      f"Answerable with valid citation: {self.answered_with_citation}/{len(answerable)} "
                      f"(need {min(MIN_ANSWERED, len(answerable))})",
                      f"Out-of-scope fallbacks: {self.out_of_scope_fallbacks}/{len(out_of_scope)}",
                      f"Accepted answers citing outside the retrieved set: {self.accepted_outside_retrieved}"]
        lines += ["", "PASS" if self.passed else "FAIL"]
        return "\n".join(lines)


def evaluate(session: Session, eval_set: EvalSet, *, embedder: Embedder, qa: QAService | None,
             min_score: float, now) -> EvalReport:
    """Run every question. The caller owns the transaction and should roll it back."""
    goal = session.scalar(select(Goal).where(Goal.code == eval_set.goal))
    if goal is None:
        raise LookupError(f"Goal {eval_set.goal!r} is not ingested; run python -m ingest.ingest first")
    learner = Learner(channel=Channel.WEB, channel_user_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
                      first_name="Evaluator", language=Language.EN, goal_id=goal.id, is_simulated=True,
                      difficulty=2, created_at=now)
    session.add(learner)
    session.flush()
    results = []
    for item in eval_set.questions:
        if qa is None:
            results.append(QuestionResult(item, retrieve(session, embedder, goal.id, item.question)))
            continue
        outcome = qa.ask(session, learner, item.question)
        results.append(QuestionResult(item, outcome.retrieved, outcome.answered,
                                      outcome.fallback_reason and outcome.fallback_reason.value,
                                      [chunk.id for chunk in outcome.cited], outcome.answer or ""))
    return EvalReport(results, min_score, retrieval_only=qa is None)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    parser.add_argument("--retrieval-only", action="store_true", help="measure retrieval without calling the LLM")
    parser.add_argument("--min-score", type=float, help="override RETRIEVAL_MIN_SCORE for this run")
    parser.add_argument("--report", type=Path, help="also write the report to this file")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from app.config import Settings
    from app.core.clock import Clock
    from app.core.embeddings import E5Embedder
    from app.db import build_engine, session_factory
    from app.llm.client import LLMClient

    settings = Settings()
    min_score = settings.retrieval_min_score if args.min_score is None else args.min_score
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    clock = Clock(sessions)
    embedder = E5Embedder(settings.embed_model)
    llm = None if args.retrieval_only else LLMClient.from_settings(settings, sessions, clock)
    qa = None if llm is None else QAService(embedder, llm, clock, min_score=min_score)
    try:
        eval_set = load_eval_set(args.eval_set)
        with sessions() as session:
            try:
                report = evaluate(session, eval_set, embedder=embedder, qa=qa, min_score=min_score,
                                  now=clock.now(session))
            finally:
                session.rollback()
    except (LookupError, OSError, ValueError) as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if llm is not None:
            llm.close()
        engine.dispose()
    text = report.format()
    print(text)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
