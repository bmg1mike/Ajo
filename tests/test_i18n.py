import re

import pytest

from app.core import i18n
from app.core.i18n import LANGUAGE_NAMES, REVIEWED, STRINGS, is_reviewed, placeholders, review_sheet, t
from app.models import Language


def test_every_string_has_all_three_languages_with_matching_placeholders():
    for key, variants in STRINGS.items():
        assert set(variants) == set(Language), key
        english = variants[Language.EN]
        for language, template in variants.items():
            assert template.strip(), (key, language)
            assert placeholders(template) == placeholders(english), (key, language)
            # Learners type commands, so they stay untranslated.
            assert re.findall(r"/\w+", template) == re.findall(r"/\w+", english), (key, language)


def test_language_names_are_endonyms_for_every_language():
    assert set(LANGUAGE_NAMES) == set(Language)
    assert LANGUAGE_NAMES[Language.YO] == "Yorùbá"


def test_unreviewed_drafts_fall_back_to_english():
    assert REVIEWED == {Language.EN}  # update together with the review sign-off in i18n.py
    for language in (Language.YO, Language.PCM):
        assert not is_reviewed(language)
        assert t("onboarding.agree", language) == "I agree"
        assert t("onboarding.joined", language, name="Ada") == t("onboarding.joined", name="Ada")


def test_reviewed_language_uses_its_own_strings(monkeypatch):
    monkeypatch.setattr(i18n, "REVIEWED", frozenset({Language.EN, Language.YO}))
    assert t("onboarding.agree", Language.YO) == "Mo gbà"
    assert t("onboarding.joined", "yo", name="Ṣadé").startswith("O ti wọlé, Ṣadé!")
    assert t("onboarding.agree", Language.PCM) == "I agree"


def test_unknown_key_or_language_fails_loudly():
    with pytest.raises(KeyError):
        t("missing.key")
    with pytest.raises(ValueError):
        t("onboarding.agree", "fr")


def test_review_sheet_lists_every_key(capsys):
    sheet = review_sheet(Language.YO)
    assert sheet.startswith("# Ajo UI strings: Yorùbá review")
    assert all(f"`{key}`" in sheet for key in STRINGS)
    assert sum(line.startswith("| `") for line in sheet.splitlines()) == len(STRINGS)
    assert i18n.main(["--review", "pcm"]) == 0
    assert "Naijá (Pidgin) review" in capsys.readouterr().out
