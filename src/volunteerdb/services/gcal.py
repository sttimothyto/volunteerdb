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

The client, the configuration, the clock and the zone are the caller's
(jobs/calendar_sync.py reads them off its Env); a failed call comes back as
an Err[External] carrying the sentence the job logs. Nothing here raises.
"""

import hashlib
import json
from datetime import datetime
from urllib.parse import quote

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import External
from ..fp import Err, Ok, Result
from ..models import AppSetting, Event
from . import google_api
from .google_api import GoogleConfig

log = structlog.get_logger(__name__)

SERVICE = "google calendar"
API = "https://www.googleapis.com/calendar/v3"
TIMEOUT = google_api.TIMEOUT
# app_setting row: {"calendar_id", "created_at", "verified_at"}
SETTING_KEY = "gcal"
# Google's fixed rule id for the "anyone" scope
PUBLIC_RULE_ID = "default"


def _failed(what: str, resp: httpx.Response) -> Err[External]:
    return google_api.failed(what, f"HTTP {resp.status_code}", service=SERVICE)


def enabled(cfg: GoogleConfig) -> bool:
    """The parish Google token is provisioned. Unlike the sheets there is no
    further setting to wait for: the calendar makes itself."""
    return cfg.configured


async def mint_token(
    client: httpx.AsyncClient, cfg: GoogleConfig
) -> Result[str, External]:
    return await google_api.mint_token(client, cfg, service=SERVICE)


def calendar_name(org: str) -> str:
    org = org.strip()
    return f"{org} events" if org else "Parish events"


# --- the calendar's public faces -------------------------------------------


def embed_url(calendar_id: str, tz: str) -> str:
    """Google's own iframe view; the calendar is public, so Google serves it.
    `tz` is the zone name the view opens in (the parish's)."""
    return (
        "https://calendar.google.com/calendar/embed"
        f"?src={quote(calendar_id)}&ctz={quote(tz)}"
    )


def public_url(calendar_id: str) -> str:
    """Opens Google Calendar with an "add this calendar" prompt for the
    signed-in Google user -- the one-click subscribe for Google users."""
    return f"https://calendar.google.com/calendar/r?cid={quote(calendar_id, safe='')}"


# --- what this system remembers about its calendar -------------------------


async def stored_calendar(session: AsyncSession) -> dict | None:
    """The app_setting row, or None before the first sync has run."""
    row = await session.get(AppSetting, SETTING_KEY)
    return dict(row.value) if row and row.value.get("calendar_id") else None


async def remember(
    session: AsyncSession,
    calendar_id: str,
    *,
    now: datetime,
    created: bool = False,
    verified: bool = False,
) -> None:
    """Upsert the row. `created` stamps created_at (a fresh calendar);
    `verified` stamps verified_at (the sharing was just checked clean)."""
    current = await stored_calendar(session) or {}
    stamp = now.isoformat(timespec="seconds")
    value = {**current, "calendar_id": calendar_id}
    if created or current.get("calendar_id") != calendar_id:
        value["created_at"] = stamp
        value.pop("verified_at", None)
    if verified:
        value["verified_at"] = stamp
    stmt = pg_insert(AppSetting).values(key=SETTING_KEY, value=value)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": stmt.excluded.value, "updated_at": now},
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
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    what: str,
    repeatable: bool = True,
    **kwargs,
) -> Result[httpx.Response, External]:
    return await google_api.send(
        client, method, url, what=what, repeatable=repeatable, service=SERVICE, **kwargs
    )


def _calendar_url(calendar_id: str) -> str:
    return f"{API}/calendars/{quote(calendar_id, safe='')}"


def _events_url(calendar_id: str) -> str:
    return f"{_calendar_url(calendar_id)}/events"


def _acl_url(calendar_id: str) -> str:
    return f"{_calendar_url(calendar_id)}/acl"


async def create_calendar(
    client: httpx.AsyncClient, token: str, *, name: str, tz: str
) -> Result[str, External]:
    """A new secondary calendar owned by the parish account; returns its id.

    Not published here: verify_readonly() adds the public rule on the same
    run, and does so again on every later run. Splitting the two means a
    failure between them leaves a remembered-but-private calendar, which the
    next run repairs, rather than a published calendar nobody remembers."""
    sent = await _call(
        client,
        "POST",
        f"{API}/calendars",
        what="calendar create",
        repeatable=False,
        headers=google_api.headers(token),
        json={"summary": name, "timeZone": tz},
    )
    if isinstance(sent, Err):
        return sent
    if sent.value.status_code not in (200, 201):
        return _failed("calendar create", sent.value)
    return Ok(sent.value.json()["id"])


async def calendar_exists(
    client: httpx.AsyncClient, token: str, calendar_id: str
) -> Result[bool, External]:
    """False when Google no longer has it (somebody deleted it by hand) --
    the one condition the sync treats as "make another"."""
    sent = await _call(
        client,
        "GET",
        _calendar_url(calendar_id),
        what="calendar get",
        headers=google_api.headers(token),
    )
    if isinstance(sent, Err):
        return sent
    if sent.value.status_code in (404, 410):
        return Ok(False)
    if sent.value.status_code != 200:
        return _failed("calendar get", sent.value)
    return Ok(True)


async def _list_acl(
    client: httpx.AsyncClient, token: str, calendar_id: str
) -> Result[list[dict], External]:
    items: list[dict] = []
    params: dict[str, str] = {"maxResults": "250"}
    while True:
        sent = await _call(
            client,
            "GET",
            _acl_url(calendar_id),
            what="acl list",
            headers=google_api.headers(token),
            params=params,
        )
        if isinstance(sent, Err):
            return sent
        if sent.value.status_code != 200:
            return _failed("acl list", sent.value)
        data = sent.value.json()
        items.extend(data.get("items", []))
        next_page = data.get("nextPageToken")
        if not next_page:
            return Ok(items)
        params["pageToken"] = next_page


async def verify_readonly(
    client: httpx.AsyncClient, token: str, calendar_id: str
) -> Result[list[str], External]:
    """Make sure the calendar is public and that nobody but the parish account
    can write to it. Returns the problems it could not fix.

    The acceptable rule set is exactly two rules: the parish account as owner
    (Google adds it at creation) and "anyone" as reader. A Workspace account's
    calendar carries a third: the calendar's own id, listed as a user-owner —
    an artefact of creation, not a person, and not a problem. A missing or
    downgraded public rule is *fixed* -- that rule is what makes the calendar
    the public thing it is documented to be. A rule that lets anybody else
    write is *reported*, not removed: it was set on purpose by somebody with
    the parish password, and deleting it would be this system deciding the
    parish's sharing for it. The scheduler's alert mail is the right voice.
    """
    listed = await _list_acl(client, token, calendar_id)
    if isinstance(listed, Err):
        return listed
    rules = listed.value
    public = next(
        (r for r in rules if r.get("scope", {}).get("type") == "default"), None
    )
    if public is None:
        published = await _publish(client, token, calendar_id)
    elif public.get("role") != "reader":
        published = await _publish(
            client, token, calendar_id, rule_id=public.get("id") or PUBLIC_RULE_ID
        )
    else:
        published = Ok(None)
    if isinstance(published, Err):
        return published

    problems: list[str] = []
    owner_seen = False
    for rule in rules:
        scope = rule.get("scope", {})
        kind, who, role = scope.get("type"), scope.get("value", ""), rule.get("role")
        if kind == "default" or role not in ("writer", "owner"):
            continue  # the public rule, or a read-only grant: harmless
        if kind == "user" and who == calendar_id:
            continue  # the calendar's own identity (Workspace lists it first)
        if kind == "user" and role == "owner" and not owner_seen:
            owner_seen = True  # the parish account itself
            continue
        problems.append(f"{kind} {who} may {'write' if role == 'writer' else 'own'}")
    return Ok(problems)


async def _publish(
    client: httpx.AsyncClient,
    token: str,
    calendar_id: str,
    *,
    rule_id: str | None = None,
) -> Result[None, External]:
    """Insert (or, given a rule id, repair) the "anyone: reader" rule."""
    if rule_id is None:
        sent = await _call(
            client,
            "POST",
            _acl_url(calendar_id),
            what="publish",
            headers=google_api.headers(token),
            json={"role": "reader", "scope": {"type": "default"}},
        )
    else:
        sent = await _call(
            client,
            "PATCH",
            f"{_acl_url(calendar_id)}/{quote(rule_id, safe='')}",
            what="publish",
            headers=google_api.headers(token),
            json={"role": "reader"},
        )
    if isinstance(sent, Err):
        return sent
    if sent.value.status_code not in (200, 201):
        return _failed("publish", sent.value)
    log.info("gcal.published", calendar_id=calendar_id, repaired=rule_id is not None)
    return Ok(None)


# --- events ----------------------------------------------------------------


def event_payload(event: Event, tz: str) -> dict:
    """What the public calendar shows: title, time, location, description —
    never slots, rosters, or names. No team path either, so repointing an
    event to a task-force team causes zero calendar churn. `tz` is the zone
    name the entry is shown in (the parish's)."""
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


async def insert(
    client: httpx.AsyncClient, token: str, calendar_id: str, payload: dict
) -> Result[str, External]:
    """Create the calendar entry; returns Google's event id."""
    sent = await _call(
        client,
        "POST",
        _events_url(calendar_id),
        what="insert",
        repeatable=False,
        headers=google_api.headers(token),
        json=payload,
    )
    if isinstance(sent, Err):
        return sent
    if sent.value.status_code not in (200, 201):
        return _failed("insert", sent.value)
    return Ok(sent.value.json()["id"])


async def patch(
    client: httpx.AsyncClient,
    token: str,
    calendar_id: str,
    google_event_id: str,
    payload: dict,
) -> Result[None, External]:
    sent = await _call(
        client,
        "PATCH",
        f"{_events_url(calendar_id)}/{quote(google_event_id, safe='')}",
        what="patch",
        headers=google_api.headers(token),
        json=payload,
    )
    if isinstance(sent, Err):
        return sent
    if sent.value.status_code != 200:
        return _failed("patch", sent.value)
    return Ok(None)


async def delete(
    client: httpx.AsyncClient, token: str, calendar_id: str, google_event_id: str
) -> Result[None, External]:
    """404/410 count as success — the entry is already gone."""
    sent = await _call(
        client,
        "DELETE",
        f"{_events_url(calendar_id)}/{quote(google_event_id, safe='')}",
        what="delete",
        headers=google_api.headers(token),
    )
    if isinstance(sent, Err):
        return sent
    if sent.value.status_code not in (200, 204, 404, 410):
        return _failed("delete", sent.value)
    return Ok(None)


async def _list_events(
    client: httpx.AsyncClient,
    token: str,
    calendar_id: str,
    time_min: datetime,
    **extra: str,
) -> Result[list[dict], External]:
    items: list[dict] = []
    params: dict[str, str] = {
        "timeMin": time_min.isoformat(),
        "maxResults": "250",
        "singleEvents": "true",
        "showDeleted": "false",
        **extra,
    }
    while True:
        sent = await _call(
            client,
            "GET",
            _events_url(calendar_id),
            what="list",
            headers=google_api.headers(token),
            params=params,
        )
        if isinstance(sent, Err):
            return sent
        if sent.value.status_code != 200:
            return _failed("list", sent.value)
        data = sent.value.json()
        items.extend(data.get("items", []))
        next_page = data.get("nextPageToken")
        if not next_page:
            return Ok(items)
        params["pageToken"] = next_page


async def list_managed(
    client: httpx.AsyncClient, token: str, calendar_id: str, time_min: datetime
) -> Result[list[dict], External]:
    """Every entry the sync ever created (vdb_managed marker) still ending
    after time_min — the orphan-GC sweep. Hand-created entries never appear."""
    return await _list_events(
        client, token, calendar_id, time_min, privateExtendedProperty="vdb_managed=1"
    )


def is_managed(item: dict) -> bool:
    return (
        item.get("extendedProperties", {}).get("private", {}).get("vdb_managed") == "1"
    )


async def list_unmanaged(
    client: httpx.AsyncClient, token: str, calendar_id: str, time_min: datetime
) -> Result[list[dict], External]:
    """Entries somebody put on the calendar by hand — anything after time_min
    without the marker. The API cannot filter on a property's absence, so
    this lists everything and drops the marked ones."""
    listed = await _list_events(client, token, calendar_id, time_min)
    if isinstance(listed, Err):
        return listed
    return Ok([item for item in listed.value if not is_managed(item)])
