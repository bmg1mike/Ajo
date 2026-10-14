"""The hand-curated opportunity list (spec sections 5 and 7.7).

`data/opportunities.json` is written and checked by a person: every entry names the official page it
came from and the day someone last verified it there. Nothing in it is generated or scraped. The
app syncs the file into the `opportunities` table at startup, so the file is the source of truth.

A weekly payout shows one opportunity that matches the learner's goal and country, whose deadline
has not passed on the virtual clock, and whose check is less than VERIFICATION_MAX_AGE_DAYS old. Of
those, the curator's `priority` decides (lowest first: a scholarship, the spec's example of a
payout, ranks above a registration notice), then the nearest deadline, then no stated deadline.
Expired or stale entries are never shown, and when none is left the payout says so instead of
filling the gap.
Check the file before a demo with:

    uv run python -m app.core.opportunities
"""
import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.i18n import t
from app.core.replies import Reply
from app.models import Goal, Language, Opportunity, OpportunityKind

# Opportunity details change between cycles; an entry nobody has re-checked for this long is not shown.
VERIFICATION_MAX_AGE_DAYS = 90
_NAMESPACE = UUID("6f3b2c1e-8d4a-4e7f-a1b9-2c5d7e9f0a13")


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    url: str
    country: str = Field(pattern=r"^[A-Z]{2}$")
    goal_codes: list[str] = Field(min_length=1)
    deadline: date | None  # only when the official page states one for the current cycle
    verified_on: date
    eligibility: str = Field(min_length=1)
    kind: OpportunityKind
    priority: int = Field(ge=1)  # 1 is offered first

    @field_validator("url")
    @classmethod
    def https_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("url must be an https link to the official page")
        return value


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    about: str = ""
    opportunities: list[Entry]

    @field_validator("opportunities")
    @classmethod
    def unique_urls(cls, entries: list[Entry]) -> list[Entry]:
        urls = [entry.url for entry in entries]
        if len(urls) != len(set(urls)):
            raise ValueError("each opportunity needs its own url")
        return entries


def load_catalog(path: Path) -> list[Entry]:
    return Catalog.model_validate(json.loads(path.read_text(encoding="utf-8"))).opportunities


def opportunity_id(entry: Entry) -> UUID:
    return uuid5(_NAMESPACE, entry.url)


def sync(session: Session, entries: Sequence[Entry]) -> int:
    """Make the table match the file: entries are updated in place by URL, and rows no longer listed go."""
    ids = []
    for entry in entries:
        row = session.get(Opportunity, opportunity_id(entry)) or Opportunity(id=opportunity_id(entry))
        for name, value in entry.model_dump().items():
            setattr(row, name, value)
        session.add(row)
        ids.append(row.id)
    session.execute(delete(Opportunity).where(Opportunity.id.not_in(ids)) if ids else delete(Opportunity))
    session.flush()
    return len(ids)


def unavailable_reason(opportunity: Opportunity, goal: Goal, today: date) -> str | None:
    """Why the opportunity can't be shown to a learner with this goal today; None if it can."""
    if opportunity.country != goal.country or goal.code not in (opportunity.goal_codes or []):
        return "other goal or country"
    if opportunity.deadline is not None and opportunity.deadline < today:
        return f"deadline {opportunity.deadline.isoformat()} has passed"
    if (today - opportunity.verified_on).days > VERIFICATION_MAX_AGE_DAYS:
        return f"last verified {opportunity.verified_on.isoformat()}, over {VERIFICATION_MAX_AGE_DAYS} days ago"
    if opportunity.verified_on > today:
        return f"verified_on {opportunity.verified_on.isoformat()} is in the future"
    return None


def choose(session: Session, goal: Goal, today: date) -> Opportunity | None:
    """The open, recently verified opportunity for the goal with the best priority, then the nearest deadline;
    None if there is none."""
    rows = session.scalars(select(Opportunity).where(Opportunity.country == goal.country)).all()
    shown = [row for row in rows if unavailable_reason(row, goal, today) is None]
    shown.sort(key=lambda row: (row.priority, row.deadline is None, row.deadline or date.max, row.title, row.id))
    return shown[0] if shown else None


def opportunity_reply(opportunity: Opportunity | None, language: Language) -> Reply:
    """Title, provider and eligibility stay as the provider wrote them, like citations."""
    if opportunity is None:
        return Reply(t("opportunity.none", language))
    deadline = (t("opportunity.deadline", language, date=opportunity.deadline.isoformat())
                if opportunity.deadline is not None else t("opportunity.no_deadline", language))
    return Reply(t("opportunity.text", language, title=opportunity.title, provider=opportunity.provider,
                   eligibility=opportunity.eligibility or "-", deadline=deadline, url=opportunity.url,
                   verified=opportunity.verified_on.isoformat()))


# Command line


def report(entries: Sequence[Entry], today: date) -> list[str]:
    lines = []
    for entry in entries:
        row = Opportunity(**entry.model_dump())
        problems = [reason for code in entry.goal_codes
                    if (reason := unavailable_reason(row, Goal(code=code, country=entry.country), today))]
        status = f"hidden: {problems[0]}" if problems else "shown"
        deadline = entry.deadline.isoformat() if entry.deadline else "none stated"
        lines.append(f"- [{entry.priority}] {entry.title} ({entry.provider}, {entry.kind.value}): deadline {deadline}, "
                     f"verified {entry.verified_on}; {status}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate data/opportunities.json and show which entries a payout "
                                                 "would offer today.")
    parser.add_argument("--file", type=Path, default=Path("data/opportunities.json"))
    parser.add_argument("--today", type=date.fromisoformat, default=date.today(),
                        help="check against this date instead of today (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    try:
        entries = load_catalog(args.file)
    except (OSError, ValueError) as error:
        print(f"{args.file}: {error}", file=sys.stderr)
        return 2
    print(f"# Opportunities on {args.today.isoformat()}\n")
    lines = report(entries, args.today)
    print("\n".join(lines) or "(the file lists no opportunities)")
    shown = sum(line.endswith("; shown") for line in lines)
    print(f"\n{shown} of {len(entries)} can be shown. Re-check each official page and update verified_on "
          f"at least every {VERIFICATION_MAX_AGE_DAYS} days.")
    return 0 if shown else 1


if __name__ == "__main__":
    raise SystemExit(main())
