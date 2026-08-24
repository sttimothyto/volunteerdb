"""Google Calendar, over raw REST.

The parish calendar is a secondary Google Calendar this system creates for
itself under the parish Google token (services/google_api.py) the roster
sheets already use, with two calendar scopes added:

    calendar.app.created  make secondary calendars, and read and write the
                          events on them -- only calendars THIS client made,
                          the drive.file shape. So the same rule holds:
                          authorise with the same OAuth client every time.
    calendar.acls         read and set who may see the calendar, which is
                          how it is made public and how that is checked.

Nothing is created by hand: jobs/calendar_sync.py calls create_calendar()
the first time it runs with a token, remembers the id in app_setting, and
on every run has verify_readonly() confirm the sharing is exactly "anyone:
reader" plus the parish account's own ownership -- re-publishing the public
rule if it is missing, and reporting any rule that would let somebody else
write.

Every pushed payload carries extendedProperties.private.vdb_id/vdb_managed,
which is how the sync tells its own entries from anything a human put on
the calendar: list_managed() filters on the marker, list_unmanaged() on its
absence. A hand-created entry is never touched -- it is reported, because
the calendar exists to mirror this system and nothing else.
"""

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import AppSetting, Event
from . import google_api

log = structlog.get_logger(__name__)

API = "https://www.googleapis.com/calendar/v3"
TIMEOUT = google_api.TIMEOUT
# app_setting row: {"calendar_id", "created_at", "verified_at"}
SETTING_KEY = "gcal"
# Google's fixed rule id for the "anyone" scope
PUBLIC_RULE_ID = "default"


class GcalError(RuntimeError):
    """A Google API call failed; the sync counts it and moves on."""


def enabled() -> bool:
    """The parish Google token is provisioned. Unlike the sheets there is no
    further setting to wait for: the calendar makes itself."""
    return google_api.configured()


async def mint_token() -> str:
    try:
        return await google_api.mint_token()
    except google_api.GoogleApiError as exc:
        raise GcalError(str(exc)) from exc


def calendar_name() -> str:
    org = settings().org_name.strip()
    return f"{org} events" if org else "Parish events"


# --- the calendar's public faces -------------------------------------------


def embed_url(calendar_id: str) -> str:
    """Google's own iframe view; the calendar is public, so Google serves it."""
    return (
        "https://calendar.google.com/calendar/embed"
        f"?src={quote(calendar_id)}&ctz={quote(settings().timezone)}"
    )


def public_url(calendar_id: str) -> str:
    """Opens Google Calendar with an "add this calendar" prompt for the
    signed-in Google user -- the one-click subscribe for Google users."""
    return f"https://calendar.google.com/calendar/r?cid={quote(calendar_id, safe='')}"


def google_ics_url(calendar_id: str) -> str:
    """The .ics feed Google publishes for a public calendar. Lags the app's
    own feed by up to a sync interval; offered for completeness."""
    return (
        "https://calendar.google.com/calendar/ical/"
        f"{quote(calendar_id, safe='')}/public/basic.ics"
    )


# --- what this system remembers about its calendar -------------------------


async def stored_calendar(session: AsyncSession) -> dict | None:
    """The app_setting row, or None before the first sync has run."""
    row = await session.get(AppSetting, SETTING_KEY)
    return dict(row.value) if row and row.value.get("calendar_id") else None


async def remember(
    session: AsyncSession,
    calendar_id: str,
    *,
    created: bool = False,
    verified: bool = False,
) -> None:
    """Upsert the row. `created` stamps created_at (a fresh calendar);
    `verified` stamps verified_at (the sharing was just checked clean)."""
    current = await stored_calendar(session) or {}
    now = datetime.now(UTC).isoformat(timespec="seconds")
    value = {**current, "calendar_id": calendar_id}
    if created or current.get("calendar_id") != calendar_id:
        value["created_at"] = now
        value.pop("verified_at", None)
    if verified:
        value["verified_at"] = now
    stmt = pg_insert(AppSetting).values(key=SETTING_KEY, value=value)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": stmt.excluded.value, "updated_at": sa.func.now()},
    )
    await session.execute(stmt)


async def forget_pushes(session: AsyncSession) -> int:
    """NULL every event's calendar stamp. For when the calendar they name no
    longer exists: the next reconcile then inserts everything afresh onto
    the replacement instead of patching entries that are gone."""
    result = await session.execute(
        sa.update(Event)
        .where(Event.google_event_id.is_not(None))
        .values(google_event_id=None, google_fingerprint=None)
    )
    return result.rowcount


# --- the wire --------------------------------------------------------------


async def _call(
    method: str, url: str, *, what: str, repeatable: bool = True, **kwargs
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            return await google_api.send(
                client, method, url, what=what, repeatable=repeatable, **kwargs
            )
        except google_api.GoogleApiError as exc:
            raise GcalError(str(exc)) from exc


def _calendar_url(calendar_id: str) -> str:
    return f"{API}/calendars/{quote(calendar_id, safe='')}"


def _events_url(calendar_id: str) -> str:
    return f"{_calendar_url(calendar_id)}/events"


def _acl_url(calendar_id: str) -> str:
    return f"{_calendar_url(calendar_id)}/acl"


async def create_calendar(token: str) -> str:
    """A new secondary calendar owned by the parish account; returns its id.

    Not published here: verify_readonly() adds the public rule on the same
    run, and does so again on every later run. Splitting the two means a
    failure between them leaves a remembered-but-private calendar, which the
    next run repairs, rather than a published calendar nobody remembers."""
    resp = await _call(
        "POST",
        f"{API}/calendars",
        what="calendar create",
        repeatable=False,
        headers=google_api.headers(token),
        json={"summary": calendar_name(), "timeZone": settings().timezone},
    )
    if resp.status_code not in (200, 201):
        raise GcalError(f"calendar create failed: HTTP {resp.status_code}")
    return resp.json()["id"]


async def calendar_exists(token: str, calendar_id: str) -> bool:
    """False when Google no longer has it (somebody deleted it by hand) --
    the one condition the sync treats as "make another"."""
    resp = await _call(
        "GET",
        _calendar_url(calendar_id),
        what="calendar get",
        headers=google_api.headers(token),
    )
    if resp.status_code in (404, 410):
        return False
    if resp.status_code != 200:
        raise GcalError(f"calendar get failed: HTTP {resp.status_code}")
    return True


async def _list_acl(token: str, calendar_id: str) -> list[dict]:
    items: list[dict] = []
    params: dict[str, str] = {"maxResults": "250"}
    while True:
        resp = await _call(
            "GET",
            _acl_url(calendar_id),
            what="acl list",
            headers=google_api.headers(token),
            params=params,
        )
        if resp.status_code != 200:
            raise GcalError(f"acl list failed: HTTP {resp.status_code}")
        data = resp.json()
        items.extend(data.get("items", []))
        next_page = data.get("nextPageToken")
        if not next_page:
            return items
        params["pageToken"] = next_page


async def verify_readonly(token: str, calendar_id: str) -> list[str]:
    """Make sure the calendar is public and that nobody but the parish account
    can write to it. Returns the problems it could not fix.

    The acceptable rule set is exactly two rules: the parish account as owner
    (Google adds it at creation) and "anyone" as reader. A missing or
    downgraded public rule is *fixed* -- that rule is what makes the calendar
    the public thing it is documented to be. A rule that lets anybody else
    write is *reported*, not removed: it was set on purpose by somebody with
    the parish password, and deleting it would be this system deciding the
    parish's sharing for it. The scheduler's alert mail is the right voice.
    """
    rules = await _list_acl(token, calendar_id)
    public = next(
        (r for r in rules if r.get("scope", {}).get("type") == "default"), None
    )
    if public is None:
        await _publish(token, calendar_id)
    elif public.get("role") != "reader":
        await _publish(token, calendar_id, rule_id=public.get("id") or PUBLIC_RULE_ID)

    problems: list[str] = []
    owner_seen = False
    for rule in rules:
        scope = rule.get("scope", {})
        kind, who, role = scope.get("type"), scope.get("value", ""), rule.get("role")
        if kind == "default" or role not in ("writer", "owner"):
            continue  # the public rule, or a read-only grant: harmless
        if kind == "user" and role == "owner" and not owner_seen:
            owner_seen = True  # the parish account itself
            continue
        problems.append(f"{kind} {who} may {'write' if role == 'writer' else 'own'}")
    return problems


async def _publish(token: str, calendar_id: str, *, rule_id: str | None = None) -> None:
    """Insert (or, given a rule id, repair) the "anyone: reader" rule."""
    if rule_id is None:
        resp = await _call(
            "POST",
            _acl_url(calendar_id),
            what="publish",
            headers=google_api.headers(token),
            json={"role": "reader", "scope": {"type": "default"}},
        )
    else:
        resp = await _call(
            "PATCH",
            f"{_acl_url(calendar_id)}/{quote(rule_id, safe='')}",
            what="publish",
            headers=google_api.headers(token),
            json={"role": "reader"},
        )
    if resp.status_code not in (200, 201):
        raise GcalError(f"publish failed: HTTP {resp.status_code}")
    log.info("gcal.published", calendar_id=calendar_id, repaired=rule_id is not None)


# --- events ----------------------------------------------------------------


def event_payload(event: Event) -> dict:
    """What the public calendar shows: title, time, location, description —
    never slots, rosters, or names. No team path either, so repointing an
    event to a task-force team causes zero calendar churn."""
    tz = settings().timezone
    payload: dict = {
        "summary": event.title,
        "start": {"dateTime": event.starts_at.isoformat(), "timeZone": tz},
        "end": {"dateTime": event.ends_at.isoformat(), "timeZone": tz},
        "extendedProperties": {
            "private": {"vdb_id": str(event.id), "vdb_managed": "1"}
        },
    }
    if event.location:
        payload["location"] = event.location
    if event.description:
        payload["description"] = event.description
    return payload


def fingerprint(payload: dict) -> str:
    """Stable digest of a payload: equal dicts hash equal regardless of key
    order, so the reconcile tells changed from current without updated_at."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


async def insert(token: str, calendar_id: str, payload: dict) -> str:
    """Create the calendar entry; returns Google's event id."""
    resp = await _call(
        "POST",
        _events_url(calendar_id),
        what="insert",
        repeatable=False,
        headers=google_api.headers(token),
        json=payload,
    )
    if resp.status_code not in (200, 201):
        raise GcalError(f"insert failed: HTTP {resp.status_code}")
    return resp.json()["id"]


async def patch(
    token: str, calendar_id: str, google_event_id: str, payload: dict
) -> None:
    resp = await _call(
        "PATCH",
        f"{_events_url(calendar_id)}/{quote(google_event_id, safe='')}",
        what="patch",
        headers=google_api.headers(token),
        json=payload,
    )
    if resp.status_code != 200:
        raise GcalError(f"patch failed: HTTP {resp.status_code}")


async def delete(token: str, calendar_id: str, google_event_id: str) -> None:
    """404/410 count as success — the entry is already gone."""
    resp = await _call(
        "DELETE",
        f"{_events_url(calendar_id)}/{quote(google_event_id, safe='')}",
        what="delete",
        headers=google_api.headers(token),
    )
    if resp.status_code not in (200, 204, 404, 410):
        raise GcalError(f"delete failed: HTTP {resp.status_code}")


async def _list_events(
    token: str, calendar_id: str, time_min: datetime, **extra: str
) -> list[dict]:
    items: list[dict] = []
    params: dict[str, str] = {
        "timeMin": time_min.isoformat(),
        "maxResults": "250",
        "singleEvents": "true",
        "showDeleted": "false",
        **extra,
    }
    while True:
        resp = await _call(
            "GET",
            _events_url(calendar_id),
            what="list",
            headers=google_api.headers(token),
            params=params,
        )
        if resp.status_code != 200:
            raise GcalError(f"list failed: HTTP {resp.status_code}")
        data = resp.json()
        items.extend(data.get("items", []))
        next_page = data.get("nextPageToken")
        if not next_page:
            return items
        params["pageToken"] = next_page


async def list_managed(token: str, calendar_id: str, time_min: datetime) -> list[dict]:
    """Every entry the sync ever created (vdb_managed marker) still ending
    after time_min — the orphan-GC sweep. Hand-created entries never appear."""
    return await _list_events(
        token, calendar_id, time_min, privateExtendedProperty="vdb_managed=1"
    )


def is_managed(item: dict) -> bool:
    return (
        item.get("extendedProperties", {}).get("private", {}).get("vdb_managed") == "1"
    )


async def list_unmanaged(
    token: str, calendar_id: str, time_min: datetime
) -> list[dict]:
    """Entries somebody put on the calendar by hand — anything after time_min
    without the marker. The API cannot filter on a property's absence, so
    this lists everything and drops the marked ones."""
    return [
        item
        for item in await _list_events(token, calendar_id, time_min)
        if not is_managed(item)
    ]
