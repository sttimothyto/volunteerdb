"""Roster import: all-or-nothing, non-destructive, dry-run capable.

- One CSV, one row per membership (common.ROSTER_HEADERS); the same format
  serves manual imports and the nightly roster-sheet sync.
- A row carrying an ID (exports always write it) is pinned to that exact
  volunteer — an unknown ID is an error, never a create — so editing an email
  next to an ID corrects the address instead of duplicating the person.
- Rows without an ID are matched by email (plus name to break
  family-shared-email ties). This is NOT a fallback chain: a row carrying an
  email that matches nobody does not then try the name, it creates a volunteer
  (with a warning if that name already exists). Only a row with a blank email
  matches by name.
- Nothing here ever removes. Both entry points (run_import, run_team_sync)
  only add and update, and a blank cell never clears a field — so a row
  missing from a file is not a departure, and the sync's write-back leg puts
  that person back into the sheet. Memberships end in the app. A row that puts
  an archived volunteer on a team does reactivate them: joining implies
  active.
- Any error rolls the whole import back; the report lists every issue.

The two entry points take the Env and open their own unit of work
(db.transaction) -- the plan's exemption for the orchestrators -- and abort
it with session.rollback() rather than an exception: a dry run, a report with
errors, and a refused import all leave the database untouched.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from io import StringIO
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..actors import load_actor
from ..db import transaction
from ..errors import DomainError, message, require
from ..fp import Err, Ok, Result
from ..log import audit_log
from ..models import AppUser, Membership, Volunteer
from ..permissions import Actor
from ..services import memberships as membership_service
from ..services import teams as team_service
from .common import ROSTER_HEADERS, ROSTER_SHEET, clean_cell, parse_role

if TYPE_CHECKING:
    from ..env import Env


@dataclass
class Issue:
    sheet: str
    row: int
    message: str


@dataclass
class ImportReport:
    volunteers_created: int = 0
    volunteers_updated: int = 0
    memberships_created: int = 0
    memberships_updated: int = 0
    volunteers_reactivated: int = 0  # membership created for an archived volunteer
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    applied: bool = False
    # (was, now) for every existing address a row redirected — filling a blank
    # one does not count. Reported, never mailed: a redirect IS the first step
    # of an account takeover and a sheet is the quietest place to do it, but a
    # single pass over a messy sheet redirects dozens at once, and mailing each
    # of those old mailboxes — usually the dead ones being fixed — spent a real
    # slice of a 200-message day (services/mail_quota.py). The volunteer_history
    # row is the durable record; the deliberate single edit still mails, from
    # the volunteer page and PATCH /api/volunteers/{id}.
    addresses_replaced: list[tuple[str, str]] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


@dataclass
class RosterRow:
    """One data row, cells already clean_cell-ed (None = blank).
    Field order after `row` must match ROSTER_HEADERS."""

    row: int
    volunteer_id: str | None
    first: str | None
    last: str | None
    email: str | None
    phone: str | None
    volunteer_notes: str | None
    team: str | None
    role: str | None


_EXTRA_COLUMNS_WARNING = (
    "extra columns (custom fields) are ignored — custom values are not imported yet"
)

_HEADERS_FOLDED = [h.casefold() for h in ROSTER_HEADERS]


def parse_roster_csv(content: bytes, report: ImportReport) -> list[RosterRow] | None:
    """Rows of the unified roster CSV; None (with report errors) if unreadable."""
    if content[:4] == b"PK\x03\x04":  # zip magic: .xlsx
        report.errors.append(
            Issue(
                ROSTER_SHEET,
                0,
                "Excel workbooks are no longer supported — export a fresh "
                "roster .csv and edit that instead",
            )
        )
        return None
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        report.errors.append(
            Issue(ROSTER_SHEET, 0, "cannot read file: not a UTF-8 .csv")
        )
        return None

    raw_rows = list(csv.reader(StringIO(text)))
    n = len(ROSTER_HEADERS)
    header = [str(c).strip().casefold() for c in raw_rows[0]] if raw_rows else []
    if header[:n] != _HEADERS_FOLDED:
        report.errors.append(
            Issue(
                ROSTER_SHEET,
                1,
                "cannot identify CSV: the header row does not match the roster "
                "template — download a fresh template or export",
            )
        )
        return None
    if any(header[n:]):
        report.warnings.append(Issue(ROSTER_SHEET, 1, _EXTRA_COLUMNS_WARNING))

    rows: list[RosterRow] = []
    for row_num, raw in enumerate(raw_rows[1:], start=2):
        cells = tuple(raw) + (None,) * (n - len(raw))
        rows.append(RosterRow(row_num, *(clean_cell(c) for c in cells[:n])))
    return rows


async def run_import(
    env: Env, content: bytes, *, dry_run: bool, user_id: int | None
) -> Result[ImportReport, DomainError]:
    """Parse and apply a roster CSV. On dry_run or any error, everything rolls
    back. Non-admin users (leaders/seconds) are scoped to the teams they
    manage; user_id=None runs unrestricted (service-level callers). The only
    Err is the refusal to import at all: row problems are in the report."""
    report = ImportReport()
    rows = parse_roster_csv(content, report)
    if rows is None:
        return Ok(report)

    async with transaction(env, user_id) as session:
        actor = None
        if user_id is not None:
            user = await session.get(AppUser, user_id)
            assert user is not None, f"unknown user {user_id}"
            actor = await load_actor(session, user)
            # The right to import at all, checked here rather than at the
            # two front doors: rows are then scoped one by one below, and
            # both surfaces reach this same function.
            if denied := require(actor.can_import_export, "import spreadsheets"):
                await session.rollback()
                return denied
        applied = await apply_rows(session, rows, report, actor)
        if isinstance(applied, Err):
            report.errors.append(Issue(ROSTER_SHEET, 0, message(applied.error)))
        if dry_run or report.has_errors:
            await session.rollback()  # the block then exits without committing
        else:
            report.applied = True
    audit_log(
        "import.finished",
        outcome=(
            "applied"
            if report.applied
            else "dry-run (rolled back)"
            if dry_run
            else "failed (rolled back)"
        ),
        volunteers_created=report.volunteers_created,
        volunteers_updated=report.volunteers_updated,
        volunteers_reactivated=report.volunteers_reactivated,
        memberships_created=report.memberships_created,
        memberships_updated=report.memberships_updated,
        errors=len(report.errors),
    )
    return Ok(report)


async def run_team_sync(
    env: Env,
    content: bytes,
    *,
    team_id: int,
    user_id: int | None,
    dry_run: bool = False,
) -> ImportReport:
    """Apply one team's roster sheet to the database. All-or-nothing: any
    error rolls the whole team back.

    Adds and updates only, like every other path through this module. Somebody
    in the database but missing from the sheet is not a departure — it is a row
    a leader deleted, a filtered view, or a botched paste — so the sync's
    write-back leg puts them back into the sheet instead. Members leave a team
    in the app, where the change is attributable and reversible.

    Scoped to the one team, not unrestricted. A sheet is edited by whoever
    holds its link and applied overnight with nobody looking, so it runs under
    an actor that owns exactly this team: rows for anywhere else are already
    refused below, and the contact columns of anyone not already on the roster
    are refused by apply_rows (see the granted_ids note there)."""
    report = ImportReport()
    rows = parse_roster_csv(content, report)
    if rows is None:
        return report
    sheet_actor = Actor(
        user=AppUser(is_admin=False),
        volunteer_id=None,
        managed_team_ids={team_id},
        people_team_ids={team_id},
        full_view_team_ids={team_id},
        names_view_team_ids=set(),
    )
    async with transaction(env, user_id) as session:
        applied = await apply_rows(
            session, rows, report, sheet_actor, sync_team_id=team_id
        )
        if isinstance(applied, Err):
            report.errors.append(Issue(ROSTER_SHEET, 0, message(applied.error)))
        if dry_run or report.has_errors:
            await session.rollback()  # the block then exits without committing
        else:
            report.applied = True
    audit_log(
        "sync.team_finished",
        team_id=team_id,
        outcome="applied" if report.applied else "failed (rolled back)",
        volunteers_created=report.volunteers_created,
        volunteers_updated=report.volunteers_updated,
        memberships_created=report.memberships_created,
        memberships_updated=report.memberships_updated,
        volunteers_reactivated=report.volunteers_reactivated,
        errors=len(report.errors),
    )
    return report


async def apply_rows(
    session: AsyncSession,
    rows: list[RosterRow],
    report: ImportReport,
    actor=None,
    *,
    sync_team_id: int | None = None,
) -> Result[None, DomainError]:
    """Upsert volunteers and memberships from parsed rows. Row problems go
    into the report; the Err is a membership write the service refused.

    actor None runs unrestricted; a non-admin actor is scoped row-by-row to
    the teams they manage. sync_team_id switches on sync mode: blank Team cells
    default to that team, and rows naming any other team are errors.
    """
    volunteers = list((await session.execute(sa.select(Volunteer))).scalars())
    by_id: dict[int, Volunteer] = {}
    by_email: dict[str, list[Volunteer]] = {}
    by_name: dict[str, list[Volunteer]] = {}

    def index(v: Volunteer) -> None:
        by_id[v.id] = v
        if v.email:
            by_email.setdefault(v.email.lower(), []).append(v)
        by_name.setdefault(v.full_name.lower(), []).append(v)

    for v in volunteers:
        index(v)

    def match(email: str | None, name: str | None) -> Volunteer | None | str:
        """A volunteer, None (no match), or an error message string."""
        candidates: list[Volunteer] = []
        if email:
            candidates = by_email.get(email.lower(), [])
            if len(candidates) > 1 and name:
                narrowed = [
                    c for c in candidates if c.full_name.lower() == name.lower()
                ]
                if narrowed:
                    candidates = narrowed
        elif name:
            candidates = by_name.get(name.lower(), [])
        if len(candidates) > 1:
            return f"ambiguous: {len(candidates)} volunteers match {email or name!r}"
        return candidates[0] if candidates else None

    def resolve_row(r: RosterRow) -> Volunteer | None | str:
        """The row's volunteer: by ID when present (authoritative, never
        creates), else by email/name. None = no match (a new volunteer);
        a str is an error message."""
        if r.volunteer_id is not None:
            try:
                vid = int(r.volunteer_id)
            except ValueError:
                return (
                    f"ID {r.volunteer_id!r} is not a number — IDs come from "
                    "exports; leave the cell blank for new people"
                )
            found = by_id.get(vid)
            if found is None:
                return (
                    f"ID {r.volunteer_id} matches no volunteer — leave the "
                    "cell blank to add a new person"
                )
            return found
        name = f"{r.first} {r.last}" if r.first and r.last else None
        return match(r.email, name)

    restricted = actor is not None and not actor.is_admin

    # every current membership in one query: the upsert loop and the
    # restricted scope check both read from this map instead of issuing
    # per-row lookups
    membership_by_pair: dict[tuple[int, int], Membership] = {
        (m.volunteer_id, m.team_id): m
        for m in (await session.execute(sa.select(Membership))).scalars()
    }
    tree = await team_service.tree(session)
    paths = tree.paths
    team_by_path = {p.lower(): tid for tid, p in paths.items()}
    team_by_name: dict[str, list[int]] = {}
    for t in tree.teams:
        team_by_name.setdefault(t.name.lower(), []).append(t.id)

    def resolve_team(team_path: str) -> int | str:
        """A team id, or an error message string."""
        team_id = team_by_path.get(team_path.lower())
        if team_id is not None:
            return team_id
        ids = team_by_name.get(team_path.lower(), [])
        if len(ids) == 1:
            return ids[0]
        if len(ids) > 1:
            return f"team name {team_path!r} is ambiguous, use its full path"
        return f"unknown team {team_path!r}"

    # Restricted imports (leaders/seconds): a row that puts someone on a
    # managed team licenses that person's volunteer columns too — an existing
    # volunteer's contact update (granted_ids, keyed by resolved id so a
    # family-shared email cannot license the other spouse), or a brand-new
    # volunteer's creation (granted_new_keys). Scope is pre-import state only.
    #
    # A SYNC sheet does not hand out granted_ids. In an interactive import the
    # leader typed the file and the reviewer sees the diff; a team's roster
    # sheet is edited by whoever holds the link, unattended and overnight, so
    # letting a pasted ID license that person's address would make the quietest
    # surface in the app the best place to redirect somebody's mail (and then
    # ask for their invite). A sync sheet may still add people — new rows and
    # memberships — but it may only rewrite the contact details of people
    # already on the team, which membership_by_pair already knows.
    granted_ids: set[int] = set()
    granted_new_keys: set[str] = set()
    teams_by_volunteer: dict[int, set[int]] = {}
    if restricted:
        for volunteer_id, team_id in membership_by_pair:
            teams_by_volunteer.setdefault(volunteer_id, set()).add(team_id)
        for r in rows:
            if not r.team and sync_team_id is None:
                continue
            # a team's own sheet may leave Team blank, exactly as the main loop
            # below reads it
            team_id = sync_team_id if not r.team else resolve_team(r.team)
            # people_team_ids: this licence is what lets a row rewrite a
            # volunteer's name, address and notes, so a task force must not
            # grant it over the members it borrowed (permissions.Actor)
            if isinstance(team_id, str) or team_id not in actor.people_team_ids:
                continue
            found = resolve_row(r)
            if isinstance(found, str):
                continue  # the main loop reports the error
            if found is None:  # only possible for blank-ID rows
                if r.email:
                    granted_new_keys.add(r.email.lower())
                name = f"{r.first} {r.last}" if r.first and r.last else None
                if name:
                    granted_new_keys.add(name.lower())
            elif sync_team_id is None or not found.is_active:
                # A sync sheet licenses nobody new (see above) with one
                # exception: an ARCHIVED volunteer is one a previous run of
                # this very sheet removed, and putting them back is the
                # documented way to undo that. Archived people are also the
                # uninteresting targets — they cannot be invited at all — so
                # the exception costs nothing the rule was protecting.
                granted_ids.add(found.id)

    for r in rows:
        if not any((r.volunteer_id, r.first, r.last, r.email, r.team, r.role)):
            continue
        if not r.first or not r.last:
            report.errors.append(
                Issue(ROSTER_SHEET, r.row, "first and last name are both required")
            )
            continue
        email = r.email.lower() if r.email else None
        full_name = f"{r.first} {r.last}"

        found = resolve_row(r)
        if isinstance(found, str):
            report.errors.append(Issue(ROSTER_SHEET, r.row, found))
            continue

        if found is None:
            # Matching is email-first with no name fallback, so a new address
            # for someone already on file silently creates a second record.
            # That is the intended rule; saying nothing about it is what made
            # the July import produce duplicates.
            if email and by_name.get(full_name.lower()):
                report.warnings.append(
                    Issue(
                        ROSTER_SHEET,
                        r.row,
                        f"{email!r} matched nobody, but {full_name!r} already exists — "
                        "creating a NEW volunteer. If they are the same person, set the "
                        "email on the existing record first.",
                    )
                )
            if restricted and not (
                (email and email in granted_new_keys)
                or full_name.lower() in granted_new_keys
            ):
                report.errors.append(
                    Issue(
                        ROSTER_SHEET,
                        r.row,
                        "new volunteers must be put on a team you lead (Team column)",
                    )
                )
                continue
            target = Volunteer(
                first_name=r.first,
                last_name=r.last,
                email=email,
                phone=r.phone,
                notes=r.volunteer_notes,
                is_active=True,
            )
            session.add(target)
            await session.flush()
            index(target)
            report.volunteers_created += 1
        else:
            # Denied even when the row changes nothing: erroring only on a
            # change would let dry-runs probe contact fields by brute force.
            if restricted and not (
                actor.can_edit_volunteer(
                    found.id, teams_by_volunteer.get(found.id, set())
                )
                or found.id in granted_ids
            ):
                report.errors.append(
                    Issue(
                        ROSTER_SHEET,
                        r.row,
                        f"not allowed to edit volunteer {email or full_name!r} "
                        "(not on a team you lead)",
                    )
                )
                continue
            # A copy-pasted row keeping a stale ID would silently rewrite an
            # unrelated volunteer; both names differing is the tell (a surname
            # change alone stays quiet).
            if (
                r.volunteer_id is not None
                and r.first.lower() != found.first_name.lower()
                and r.last.lower() != found.last_name.lower()
            ):
                report.warnings.append(
                    Issue(
                        ROSTER_SHEET,
                        r.row,
                        f"ID {r.volunteer_id} is {found.full_name!r} on file "
                        f"but this row says {full_name!r} — check the ID",
                    )
                )
            old_email = found.email
            changed = False
            for attr, value in (
                ("first_name", r.first),
                ("last_name", r.last),
                ("email", email),
                ("phone", r.phone),
                ("notes", r.volunteer_notes),
            ):
                if value is not None and getattr(found, attr) != value:
                    setattr(found, attr, value)
                    changed = True
            if changed:
                report.volunteers_updated += 1
            if email and email != (old_email or "").lower():
                # an ID row just corrected the email: a later blank-ID row
                # carrying the new address must match this record, not create
                by_email.setdefault(email, []).append(found)
                if old_email:  # replaced, not filled in — tell the old mailbox
                    report.addresses_replaced.append((old_email.lower(), email))
            target = found

        # --- membership columns ---
        if r.team is None and sync_team_id is None:
            if r.role is not None:
                report.errors.append(
                    Issue(ROSTER_SHEET, r.row, "Team is required to assign a role")
                )
            continue  # volunteer-only row
        if r.team is None:
            team_id = sync_team_id  # a team's own sheet may leave Team blank
        else:
            team_id = resolve_team(r.team)
            if isinstance(team_id, str):
                report.errors.append(Issue(ROSTER_SHEET, r.row, team_id))
                continue
        if sync_team_id is not None and team_id != sync_team_id:
            report.errors.append(
                Issue(
                    ROSTER_SHEET,
                    r.row,
                    f"this sheet only manages {paths[sync_team_id]!r} — "
                    f"remove the {r.team!r} row",
                )
            )
            continue
        if restricted and team_id not in actor.managed_team_ids:
            report.errors.append(
                Issue(
                    ROSTER_SHEET,
                    r.row,
                    f"not allowed: {r.team!r} is not a team you lead",
                )
            )
            continue

        if r.role is None:
            report.errors.append(
                Issue(ROSTER_SHEET, r.row, "Role is required when Team is set")
            )
            continue
        role = parse_role(r.role)
        if role is None:
            report.errors.append(Issue(ROSTER_SHEET, r.row, f"unknown role {r.role!r}"))
            continue

        existing = membership_by_pair.get((target.id, team_id))
        before = existing.role if existing else None
        assigned = await membership_service.assign(
            session,
            None,  # the row's licence was already checked above
            target.id,
            team_id,
            role,
            existing=existing,
        )
        if isinstance(assigned, Err):
            return assigned
        membership = assigned.value
        if existing is None:
            # a later sheet row for the same pair must hit the update branch
            membership_by_pair[(target.id, team_id)] = membership
            report.memberships_created += 1
            if not target.is_active:
                # joining a team implies active. Creation only: re-importing an
                # export that still lists an archived member must not
                # resurrect them.
                target.is_active = True
                report.volunteers_reactivated += 1
        elif before != membership.role:
            report.memberships_updated += 1
    return Ok(None)
