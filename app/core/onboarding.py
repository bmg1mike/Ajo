"""Onboarding (spec section 7.1): /start, consent, language, first name, goal, demo circle.

Nothing is stored before the learner taps "I agree". Progress after consent lives in the
`onboarding` table under the salted channel hash; the learner row is created only once every
answer is in, and the progress row is removed in the same transaction. Every learner starts at
difficulty level 2 with no placement test, and nothing is gated behind a level.
"""
import unicodedata
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.adapters.base import Button
from app.core import circles
from app.core.i18n import LANGUAGE_NAMES, is_reviewed, t
from app.core.replies import Reply
from app.models import Channel, Circle, Goal, Language, Learner, Onboarding, OnboardingStep

STARTING_DIFFICULTY = 2
MIN_NAME_CHARS = 2  # a lone initial is not a first name the circle can use
MAX_NAME_CHARS = 30
MAX_NAME_WORDS = 2  # "Mary Jane" is a first name; "My name is Amaka" is not.
_NAME_PUNCTUATION = frozenset("'-.’")

# Button payloads, well under Telegram's 64-byte callback limit.
PREFIX = "ob:"
AGREE = "ob:agree"
CHOOSE_LANGUAGE = "ob:lang:"
CHOOSE_GOAL = "ob:goal:"


def start(session: Session, user_hash: str) -> list[Reply]:
    """Explain Ajo and ask for consent. Restarting discards any unfinished progress."""
    session.execute(delete(Onboarding).where(Onboarding.channel_user_hash == user_hash))
    return [Reply(t("onboarding.intro")), consent_prompt()]


def consent_prompt() -> Reply:
    return Reply(t("onboarding.consent"), [Button(t("onboarding.agree"), AGREE)])


def language_menu(language: Language, payload_prefix: str = CHOOSE_LANGUAGE) -> Reply:
    return Reply(t("onboarding.choose_language", language),
                 [Button(LANGUAGE_NAMES[item], f"{payload_prefix}{item.value}") for item in Language])


def unreviewed_note(language: Language) -> list[Reply]:
    """Say so when a learner picks a language whose UI strings still await native-speaker review."""
    if is_reviewed(language):
        return []
    return [Reply(t("language.unreviewed", language, language_name=LANGUAGE_NAMES[language]))]


def handle_button(session: Session, state: Onboarding | None, payload: str, *, channel: Channel, user_hash: str,
                  chat_id: str, now: datetime) -> list[Reply]:
    if payload == AGREE:
        return _agree(session, state, channel=channel, user_hash=user_hash, now=now)
    if state is None:
        return [Reply(t("onboarding.consent_required")), consent_prompt()]
    if payload.startswith(CHOOSE_LANGUAGE):
        return _choose_language(session, state, payload.removeprefix(CHOOSE_LANGUAGE), now)
    if payload.startswith(CHOOSE_GOAL):
        return _choose_goal(session, state, payload.removeprefix(CHOOSE_GOAL), chat_id=chat_id, now=now)
    return [Reply(t("button.expired", state.language)), *prompt(session, state)]


def resume(session: Session, state: Onboarding | None, text: str | None, now: datetime) -> list[Reply]:
    """Input from someone who hasn't finished onboarding: a name if one is due, else the current step again."""
    if state is None:
        return [Reply(t("onboarding.start_first"))]
    if state.step == OnboardingStep.NAME and text is not None:
        return _submit_name(session, state, text, now)
    return prompt(session, state)


def prompt(session: Session, state: Onboarding) -> list[Reply]:
    if state.step == OnboardingStep.LANGUAGE:
        return [language_menu(state.language)]
    if state.step == OnboardingStep.NAME:
        return [Reply(t("onboarding.ask_name", state.language))]
    return [_goal_menu(session, state.language)]


def clean_first_name(text: str) -> str | None:
    """A first name in letters (any script, with combining tone marks), or None if it doesn't look like one."""
    name = " ".join(unicodedata.normalize("NFC", text).split())
    letters = sum(unicodedata.category(char).startswith("L") for char in name)  # "Ẹ́" is one letter, two code points
    if letters < MIN_NAME_CHARS or len(name) > MAX_NAME_CHARS or len(name.split()) > MAX_NAME_WORDS:
        return None
    if not unicodedata.category(name[0]).startswith("L"):
        return None
    if any(not (unicodedata.category(char)[0] in "LM" or char == " " or char in _NAME_PUNCTUATION) for char in name):
        return None
    return name


def _agree(session: Session, state: Onboarding | None, *, channel: Channel, user_hash: str,
           now: datetime) -> list[Reply]:
    if state is not None:
        return prompt(session, state)  # a repeated tap keeps the learner where they are
    state = Onboarding(channel_user_hash=user_hash, channel=channel, step=OnboardingStep.LANGUAGE,
                       language=Language.EN, consented_at=now, updated_at=now)
    session.add(state)
    session.flush()
    return prompt(session, state)


def _choose_language(session: Session, state: Onboarding, value: str, now: datetime) -> list[Reply]:
    try:
        language = Language(value)
    except ValueError:
        return [Reply(t("button.expired", state.language)), language_menu(state.language)]
    # The language may be changed again at any point before onboarding finishes.
    state.language, state.updated_at = language, now
    if state.step == OnboardingStep.LANGUAGE:
        state.step = OnboardingStep.NAME
    return [*unreviewed_note(language), *prompt(session, state)]


def _submit_name(session: Session, state: Onboarding, text: str, now: datetime) -> list[Reply]:
    name = clean_first_name(text)
    if name is None:
        return [Reply(t("onboarding.invalid_name", state.language, min=MIN_NAME_CHARS, max=MAX_NAME_CHARS))]
    state.first_name, state.step, state.updated_at = name, OnboardingStep.GOAL, now
    return [_goal_menu(session, state.language)]


def _goal_menu(session: Session, language: Language) -> Reply:
    goals = session.scalars(select(Goal).order_by(Goal.title, Goal.code)).all()
    if not goals:
        return Reply(t("onboarding.no_goals", language))
    return Reply(t("onboarding.choose_goal", language),
                 [Button(goal.title, f"{CHOOSE_GOAL}{goal.id}") for goal in goals])


def _choose_goal(session: Session, state: Onboarding, value: str, *, chat_id: str, now: datetime) -> list[Reply]:
    if state.step != OnboardingStep.GOAL or state.first_name is None:
        return prompt(session, state)
    try:
        goal = session.get(Goal, UUID(value))
    except ValueError:
        goal = None
    if goal is None:
        return [Reply(t("button.expired", state.language)), _goal_menu(session, state.language)]
    learner = Learner(channel=state.channel, channel_user_hash=state.channel_user_hash, channel_chat_id=chat_id,
                      first_name=state.first_name, language=state.language, goal_id=goal.id, is_simulated=False,
                      share_scores=False, difficulty=STARTING_DIFFICULTY, consented_at=state.consented_at,
                      created_at=now)
    session.add(learner)
    session.delete(state)
    session.flush()
    circle = circles.demo_circle(session, goal, now)
    circles.join(session, circle, learner, now)
    return [Reply(t("onboarding.joined", learner.language, name=learner.first_name)),
            welcome_update(session, circle, learner.language),
            Reply(t("help", learner.language))]


def welcome_update(session: Session, circle: Circle, language: Language) -> Reply:
    """The circle update sent on joining: members' first names and nothing else."""
    names = circles.member_first_names(session, circle.id)
    if len(names) <= 1:
        return Reply(t("circle.welcome_alone", language, circle=circle.name))
    return Reply(t("circle.welcome", language, circle=circle.name, names=", ".join(names)))
