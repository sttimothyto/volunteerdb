"""Google Calendar, over raw REST.

The parish calendar is an ordinary Google Calendar owned by the parish
Google account, created and made public by hand
(docs/how-to/google-calendar-sync.md); this module is the thin client
jobs/calendar_sync.py pushes through. Raw httpx against the v3 endpoints —
the same no-client-library choice as the host's decorate-sheets script —
because four verbs do not justify a dependency tree.

Every pushed payload carries extendedProperties.private.vdb_id/vdb_managed,
which is how the sync tells its own entries from anything a human put on
the calendar: list_managed() filters on the marker, so a hand-created entry
is never touched, let alone deleted.
"""

import hashlib
import json
from datetime import datetime
from urllib.parse import quote

import httpx

from ..config import settings
from ..models import Event

API = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TIMEOUT = 20.0


class GcalError(RuntimeError):
    """A Google API call failed; the sync counts it and moves on."""


def enabled() -> bool:
    s = settings()
    return bool(
        s.gcal_client_id
        and s.gcal_client_secret
        and s.gcal_refresh_token
        and s.gcal_calendar_id
    )


def embed_url() -> str | None:
    """The iframe src for /events; needs only the calendar id (the calendar
    itself is public — Google serves the embed, not us)."""
    s = settings()
    if not s.gcal_calendar_id:
        return None
    return (
        "https://calendar.google.com/calendar/embed"
        f"?src={quote(s.gcal_calendar_id)}&ctz={quote(s.timezone)}"
    )


async def mint_token() -> str:
    """A fresh access token from the stored refresh token — the pattern the
    host's decorate-sheets script uses against the same endpoint."""
    s = settings()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "client_id": s.gcal_client_id,
                "client_secret": s.gcal_client_secret,
                "refresh_token": s.gcal_refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code != 200:
        raise GcalError(f"token mint failed: HTTP {resp.status_code}")
    return resp.json()["access_token"]


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


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _events_url() -> str:
    return f"{API}/calendars/{quote(settings().gcal_calendar_id)}/events"


async def insert(token: str, payload: dict) -> str:
    """Create the calendar entry; returns Google's event id."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(_events_url(), headers=_headers(token), json=payload)
    if resp.status_code not in (200, 201):
        raise GcalError(f"insert failed: HTTP {resp.status_code}")
    return resp.json()["id"]


async def patch(token: str, google_event_id: str, payload: dict) -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.patch(
            f"{_events_url()}/{quote(google_event_id)}",
            headers=_headers(token),
            json=payload,
        )
    if resp.status_code != 200:
        raise GcalError(f"patch failed: HTTP {resp.status_code}")


async def delete(token: str, google_event_id: str) -> None:
    """404/410 count as success — the entry is already gone."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.delete(
            f"{_events_url()}/{quote(google_event_id)}", headers=_headers(token)
        )
    if resp.status_code not in (200, 204, 404, 410):
        raise GcalError(f"delete failed: HTTP {resp.status_code}")


async def list_managed(token: str, time_min: datetime) -> list[dict]:
    """Every entry the sync ever created (vdb_managed marker) still ending
    after time_min — the orphan-GC sweep. Hand-created entries never appear."""
    items: list[dict] = []
    params: dict[str, str] = {
        "privateExtendedProperty": "vdb_managed=1",
        "timeMin": time_min.isoformat(),
        "maxResults": "250",
        "singleEvents": "true",
        "showDeleted": "false",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        while True:
            resp = await client.get(
                _events_url(), headers=_headers(token), params=params
            )
            if resp.status_code != 200:
                raise GcalError(f"list failed: HTTP {resp.status_code}")
            data = resp.json()
            items.extend(data.get("items", []))
            next_page = data.get("nextPageToken")
            if not next_page:
                return items
            params["pageToken"] = next_page
