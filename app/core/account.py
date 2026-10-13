"""Learner-controlled settings: /language, /share_scores and confirmed /delete_me (spec sections 7.8, 10.2)."""
from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.adapters.base import Button
from app.core import circles, onboarding
from app.core.i18n import LANGUAGE_NAMES, t
from app.core.privacy import delete_learner
from app.core.replies import Reply
from app.models import Language, Learner, Onboarding

PREFIX = "acct:"
SET_LANGUAGE = "acct:lang:"
SHARE_SCORES = "acct:scores:on"
KEEP_SCORES_PRIVATE = "acct:scores:off"
DELETE_YES = "acct:del:yes"
DELETE_NO = "acct:del:no"


def language_menu(learner: Learner) -> Reply:
    return onboarding.language_menu(learner.language, SET_LANGUAGE)


def set_language(learner: Learner, value: str) -> list[Reply]:
    try:
        language = Language(value)
    except ValueError:
        return [Reply(t("button.expired", learner.language)), language_menu(learner)]
    learner.language = language
    return [Reply(t("language.changed", language, language_name=LANGUAGE_NAMES[language])),
            *onboarding.unreviewed_note(language)]


def scores_menu(learner: Learner) -> Reply:
    status = "scores.status_shared" if learner.share_scores else "scores.status_private"
    return Reply(t(status, learner.language), [Button(t("scores.share", learner.language), SHARE_SCORES),
                                               Button(t("scores.keep_private", learner.language), KEEP_SCORES_PRIVATE)])


def set_share_scores(learner: Learner, share: bool) -> Reply:
    """Explicit opt-in only: the feed shows scores just for learners who chose to share them."""
    learner.share_scores = share
    return Reply(t("scores.status_shared" if share else "scores.status_private", learner.language))


def confirm_delete(learner: Learner | None, state: Onboarding | None) -> Reply:
    language = _language(learner, state)
    if learner is None and state is None:
        return Reply(t("delete.nothing_stored", language))
    return Reply(t("delete.confirm", language), [Button(t("delete.yes", language), DELETE_YES),
                                                 Button(t("delete.cancel", language), DELETE_NO)])


def delete_me(session: Session, learner: Learner | None, user_hash: str, now: datetime) -> Reply:
    """Delete the authenticated user's data after they tapped the confirmation button.

    The circle is told only that "a member" left: the event has no actor and no payload.
    """
    language = _language(learner, session.get(Onboarding, user_hash))
    removed = session.execute(delete(Onboarding).where(Onboarding.channel_user_hash == user_hash)).rowcount > 0
    if learner is not None:
        circle_ids = circles.learner_circle_ids(session, learner.id)
        removed = delete_learner(session, learner.id, confirmed=True) or removed
        circles.record_member_left(session, circle_ids, now)
    return Reply(t("delete.done" if removed else "delete.nothing_stored", language))


def cancel_delete(learner: Learner | None, state: Onboarding | None) -> Reply:
    return Reply(t("delete.cancelled", _language(learner, state)))


def _language(learner: Learner | None, state: Onboarding | None) -> Language:
    if learner is not None:
        return learner.language
    return state.language if state is not None else Language.EN
