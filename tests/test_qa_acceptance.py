"""Milestone 2 completion check against the ingested corpus, the local model and the configured LLM.

Opt-in because it needs the database from `python -m ingest.ingest` and LLM settings in .env
(or a warm LLM cache): AJO_QA_EVAL=1 uv run pytest -m qa_eval
"""
import os

import pytest

from app.core import qa_eval

pytestmark = [
    pytest.mark.qa_eval,
    pytest.mark.skipif(os.getenv("AJO_QA_EVAL") != "1", reason="set AJO_QA_EVAL=1 to run the QA evaluation set"),
]


def test_qa_evaluation_set_meets_milestone_two(capsys):
    exit_code = qa_eval.main([])
    report = capsys.readouterr().out
    assert exit_code == 0, report
    assert "Accepted answers citing outside the retrieved set: 0" in report
