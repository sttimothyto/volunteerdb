"""Roster import: all-or-nothing, non-destructive, dry-run capable.

- One CSV, one row per membership (common.ROSTER_HEADERS); the same format
  serves manual imports and the nightly Drive sync.
- A row carrying an ID (exports always write it) is pinned to that exact
  volunteer — an unknown ID is an error, never a create — so editing an email
  next to an ID corrects the address instead of duplicating the person.
- Rows without an ID are matched by email (plus name to break
  family-shared-email ties). This is NOT a fallback chain: a row carrying an
  email that matches nobody does not then try the name, it creates a volunteer
  (with a warning if that name already exists). Only a row with a blank email
  matches by name.
- Manual imports (run_import) only add and update; rows never delete existing
  memberships. The Drive sync (run_team_sync) treats a team's sheet as that
  team's complete roster: memberships absent from the sheet are removed (the
  history tables retain them) and a volunteer losing their last membership
  anywhere is archived. In either mode, a row that puts an archived volunteer
  on a team reactivates them — joining implies active.
- Any error rolls the whole import back; the report lists every issue.
"""

import csv
from dataclasses import dataclass, field
from io import StringIO

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import db_session
from ..log import audit_log
from ..models import AppUser, Membership, Volunteer
from ..permissions import Actor, load_actor
from ..services import memberships as membership_service
from ..services import teams as team_service
from .common import ROSTER_HEADERS, ROSTER_SHEET, clean_cell, parse_role


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
    memberships_removed: int = 0  # sync mode only
    volunteers_archived: int = 0  # sync mode only
    volunteers_reactivated: int = 0  # membership created for an archived volunteer
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    applied: bool = False
    # (was, now) for every existing address a row redirected — filling a blank
    # one does not count. The caller mails the old address after the commit:
    # a redirected address is the first step of an account takeover, and a
    # sheet is the quietest place to do it. See services.mail.address_edited_email.
    addresses_replaced: list[tuple[str, str]] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def churn_suspected(self) -> bool:
        """Created and removed people in the same run: the signature of an
        edited email cell or a deleted-and-retyped row duplicating a person."""
        return bool(self.volunteers_created and self.memberships_removed)


class _Abort(Exception):
    pass


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

# Sync-mode circuit breaker: a truncated or half-filled sheet must not wipe a
# roster. Removals are refused when they would drop more than half the team
# and at least this many memberships.
SYNC_REMOVAL_MIN = 3


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
    if header[:n] != [h.casefold() for h in ROSTER_HEADERS]:
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
    content: bytes, *, dry_run: bool, user_id: int | None
) -> ImportReport:
    """Parse and apply a roster CSV. On dry_run or any error, everything rolls
    back. Non-admin users (leaders/seconds) are scoped to the teams they
    manage; user_id=None runs unrestricted (service-level callers)."""
    report = ImportReport()
    rows = parse_roster_csv(content, report)
    if rows is None:
        return report

    try:
        async with db_session(user_id) as session:
            actor = None
            if user_id is not None:
                user = await session.get(AppUser, user_id)
                assert user is not None, f"unknown user {user_id}"
                actor = await load_actor(session, user)
            await apply_rows(session, rows, report, actor)
            if dry_run or report.has_errors:
                raise _Abort()
            report.applied = True
    except _Abort:
        pass
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
    return report


async def run_team_sync(
    content: bytes, *, team_id: int, user_id: int | None, dry_run: bool = False
) -> ImportReport:
    """Apply one team's Drive sheet as that team's complete roster (upserts
    plus removals). All-or-nothing: any error — including the removal safety
    threshold — rolls the whole team back.

    Scoped to the one team, not unrestricted. A sheet is edited by whoever
    holds its Drive share and applied overnight with nobody looking, so it runs
    under an actor that owns exactly this team: rows for anywhere else are
    already refused below, and the contact columns of anyone not already on the
    roster are refused by apply_rows (see the granted_ids note there)."""
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
    try:
        async with db_session(user_id) as session:
            await apply_rows(session, rows, report, sheet_actor, sync_team_id=team_id)
            if dry_run or report.has_errors:
                raise _Abort()
            report.applied = True
    except _Abort:
        pass
    audit_log(
        "sync.team_finished",
        team_id=team_id,
        outcome="applied" if report.applied else "failed (rolled back)",
        volunteers_created=report.volunteers_created,
        volunteers_updated=report.volunteers_updated,
        memberships_created=report.memberships_created,
        memberships_updated=report.memberships_updated,
        memberships_removed=report.memberships_removed,
        volunteers_archived=report.volunteers_archived,
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
) -> None:
    """Upsert volunteers and memberships from parsed rows.

    actor None runs unrestricted; a non-admin actor is scoped row-by-row to
    the teams they manage. sync_team_id switches on sync mode: blank Team
    cells default to that team, rows for any other team are errors, and
    memberships of that team absent from the rows are removed afterwards
    (never when any row errored — an unparsable row must not read as absent).
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

    # every current membership in one query: the upsert loop, the restricted
    # scope check and the sync removal pass all read from this map instead of
    # issuing per-row lookups
    membership_by_pair: dict[tuple[int, int], Membership] = {
        (m.volunteer_id, m.team_id): m
        for m in (await session.execute(sa.select(Membership))).scalars()
    }
    presync_members: dict[int, Membership] = {
        vol_id: m
        for (vol_id, team_id), m in membership_by_pair.items()
        if team_id == sync_team_id
    }

    all_teams = await team_service.list_all(session)
    paths = team_service.team_paths(all_teams)
    team_by_path = {p.lower(): tid for tid, p in paths.items()}
    team_by_name: dict[str, list[int]] = {}
    for t in all_teams:
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
    # leader typed the file and the reviewer sees the diff; a team's Drive
    # sheet is edited by whoever holds the share, unattended and overnight, so
    # letting a pasted ID license that person's address would make the quietest
    # surface in the app the best place to redirect somebody's mail (and then
    # ask for their invite). A sync sheet may still add people — new rows and
    # memberships — but it may only rewrite the contact details of people who
    # were already on the team, which presync_members already knows.
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

    kept_volunteer_ids: set[int] = set()

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
        membership = await membership_service.assign(
            session,
            None,  # the row's licence was already checked above
            target.id,
            team_id,
            role,
            existing=existing,
        )
        if existing is None:
            # a later sheet row for the same pair must hit the update branch
            membership_by_pair[(target.id, team_id)] = membership
            report.memberships_created += 1
            if not target.is_active:
                # joining a team implies active — the mirror of the sync
                # auto-archive below. Creation only: re-importing an export
                # that still lists an archived member must not resurrect them.
                target.is_active = True
                report.volunteers_reactivated += 1
        elif before != membership.role:
            report.memberships_updated += 1
        if sync_team_id is not None:
            kept_volunteer_ids.add(target.id)

    # --- sync mode: the sheet is the whole roster, so absence means removal ---
    if sync_team_id is None or report.has_errors:
        return
    if not kept_volunteer_ids and presync_members:
        # Stronger than the percentage breaker below: a header-only download
        # (or a sheet emptied by accident) must never wipe a team, however
        # small the team is.
        report.errors.append(
            Issue(
                ROSTER_SHEET,
                0,
                f"the sheet lists nobody, but {paths[sync_team_id]!r} has "
                f"{len(presync_members)} member(s) — refusing to empty the "
                "team. If this is intentional, remove them in the app instead.",
            )
        )
        return
    to_remove = [
        m for vid, m in presync_members.items() if vid not in kept_volunteer_ids
    ]
    if len(to_remove) >= SYNC_REMOVAL_MIN and len(to_remove) * 2 > len(presync_members):
        report.errors.append(
            Issue(
                ROSTER_SHEET,
                0,
                f"refusing to remove {len(to_remove)} of "
                f"{len(presync_members)} members — over the safety threshold. "
                "If this is intentional, remove them in the app instead.",
            )
        )
        return
    removed_ids: list[int] = []
    for m in to_remove:
        removed_ids.append(m.volunteer_id)
        await membership_service.remove(session, None, m.id)  # scope checked above
        report.memberships_removed += 1
    # archive volunteers left with no membership at all: one grouped query
    # over everyone just removed (remove() flushed the deletes, so this sees
    # the post-removal state) instead of a COUNT per removal
    still_serving = set(
        await session.scalars(
            sa.select(Membership.volunteer_id)
            .where(Membership.volunteer_id.in_(removed_ids))
            .distinct()
        )
    )
    to_archive = [vid for vid in removed_ids if vid not in still_serving]
    if to_archive:
        for volunteer in await session.scalars(
            sa.select(Volunteer).where(Volunteer.id.in_(to_archive))
        ):
            volunteer.is_active = False
            report.volunteers_archived += 1
        await session.flush()
    if report.churn_suspected:
        report.warnings.append(
            Issue(
                ROSTER_SHEET,
                0,
                f"this sync both created {report.volunteers_created} "
                f"volunteer(s) and removed {report.memberships_removed} "
                "member(s) — if an email was edited or a row deleted and "
                "re-typed, the same person may now exist twice. Check the "
                "roster for duplicates.",
            )
        )
