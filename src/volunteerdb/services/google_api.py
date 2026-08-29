"""The parish Google token, and the one way requests to Google are sent.

One OAuth grant serves both Google integrations: the roster sheets
(services/gsheets.py, scopes spreadsheets + drive.file) and the parish
calendar (services/gcal.py, scopes calendar.app.created + calendar.acls).
The settings are still named VDB_SHEETS_* -- they were provisioned under
that name first, and a rename would buy nothing but an edit to the
production env file -- but what they hold is *the parish Google token*, and
this module is where that is spelled out: GoogleConfig is those settings as
a value, read once at the edge (env.Env.google()) and passed in, so nothing
under services/ reads configuration itself.

Raw httpx, no client library: between them the two callers use a dozen
verbs, which does not justify a dependency tree. Every call goes through
send(), which retries the statuses Google uses to mean "later" -- the quotas
are per-minute per-user, and a busy night's write-back can reach them. The
client is the caller's (env.HttpClients gives the edges one), which is what
lets a test route every request through a MockTransport without patching.

A failed call is a value, never an exception: External, carrying the very
sentence a leader reads on the team page ("export failed: HTTP 429"). Each
integration names itself as the service so the two are told apart in a log.
"""

import asyncio
from dataclasses import dataclass

import httpx

from ..errors import External
from ..fp import Err, Ok, Result

TOKEN_URL = "https://oauth2.googleapis.com/token"
TIMEOUT = 20.0
# Retry policy. 429 is the one that actually bites: the quotas are per-minute
# per-user, and a night where most rosters changed can brush them -- so the
# delays have to outlast a quota window rather than a blip. 2s, 8s, 32s does;
# the usual 1-2-4 does not. Worst case for one call is ~42s, and a night that
# spends it on every sheet is a night that was over quota anyway: the backoff
# is what paces the job back under the limit.
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 2.0
RETRY_FACTOR = 4.0
RETRY_MAX_DELAY = 45.0
# 429 means the request was refused without being performed, so it is safe to
# replay whatever the call does. A 5xx may arrive *after* Google acted on it,
# which is why the calls that are not replayable only retry on 429.
REFUSED = frozenset({429})
RETRY_STATUSES = REFUSED | frozenset({500, 502, 503, 504})
# Drive does not always spell throttling 429: it also reports it as a 403
# carrying one of these reasons. Those are refusals too, so they retry
# whatever the call is -- but only when the body says so, because the other
# 403 means "you may not touch this file" and must be reported at once.
RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "sharingRateLimitExceeded"}
)
SERVICE = "google"


@dataclass(frozen=True, slots=True)
class GoogleConfig:
    """The parish Google grant as values -- what VDB_SHEETS_* holds."""

    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    folder_id: str = ""  # the roster folder: the sheets need it, the calendar does not

    @property
    def configured(self) -> bool:
        """The token is provisioned: client id, secret and refresh token. Each
        integration adds its own further requirement (a folder id for the
        sheets); this is the part they share."""
        return bool(self.client_id and self.client_secret and self.refresh_token)


def failed(what: str, detail: str, *, service: str = SERVICE) -> Err[External]:
    """The one error shape for a call that did not go through: the leg that
    failed and how, in the sentence the team page and the job log show."""
    return Err(External(service, f"{what} failed: {detail}"))


def is_rate_limited(resp: httpx.Response) -> bool:
    """A 403 that is really a quota bounce rather than a permission problem.

    The distinction matters in both directions: retrying a genuine 403 just
    delays the sharing advice a leader needs, and *not* retrying a throttling
    403 wastes the sync it was meant to absorb. Only the body can tell them
    apart. A non-JSON 403 is not one of these -- the anonymous CSV export
    endpoint answers an unshared sheet with HTML, and that is the sharing case.
    """
    if resp.status_code != 403:
        return False
    try:
        error = resp.json().get("error", {})
    except ValueError:
        return False
    if error.get("status") == "RESOURCE_EXHAUSTED":
        return True
    return any(e.get("reason") in RATE_LIMIT_REASONS for e in error.get("errors", []))


def retry_after(resp: httpx.Response, fallback: float) -> float:
    """Google's own Retry-After when it sends a usable one, else our backoff.

    Capped either way: a header we misread must not park the nightly job for
    an hour.
    """
    try:
        return min(float(resp.headers.get("Retry-After", "")), RETRY_MAX_DELAY)
    except ValueError:
        return fallback


async def send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    what: str,
    repeatable: bool = True,
    service: str = SERVICE,
    **kwargs,
) -> Result[httpx.Response, External]:
    """One request, retried while Google says "later".

    Returns the response rather than judging its status: every leg reports
    failure differently -- read_csv turns a 403 into sharing advice -- and
    centralising that would flatten the messages leaders actually read. The
    last attempt's response comes back as-is, so an exhausted retry ends in
    the caller's usual "... failed: HTTP 429" instead of a new error shape.
    Only a request that never got an answer is an Err here: a reset or a read
    timeout, named after the exception, so an interactive "Sync now" records
    it against the team like any other sheet problem.

    A throttling 403 (see is_rate_limited) is retried like a 429 whatever
    the call, since it too was refused rather than half-performed.

    `repeatable=False` marks a call that must not be replayed after a 5xx:
    files.create, :batchUpdate and calendars.insert all *do* something, and a
    500 can arrive after Google already did it, leaving a duplicate sheet, a
    doubled protected range or a second calendar. Those still retry a 429,
    which is a refusal, not a half-done write.

    Backoff is deterministic, with no jitter: jitter spreads a herd of
    clients, and this is one nightly job -- a predictable delay is one a test
    can assert on.
    """
    statuses = RETRY_STATUSES if repeatable else REFUSED
    backoff = RETRY_BASE_DELAY
    for _ in range(RETRY_ATTEMPTS - 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            # A reset or a read timeout is precisely what backoff is for --
            # unless the call must not be replayed.
            if not repeatable:
                return failed(what, type(exc).__name__, service=service)
            wait = backoff
        else:
            if resp.status_code not in statuses and not is_rate_limited(resp):
                return Ok(resp)
            wait = retry_after(resp, backoff)
        await asyncio.sleep(wait)
        backoff = min(backoff * RETRY_FACTOR, RETRY_MAX_DELAY)
    try:
        return Ok(await client.request(method, url, **kwargs))
    except httpx.TransportError as exc:
        return failed(what, type(exc).__name__, service=service)


async def mint_token(
    client: httpx.AsyncClient, cfg: GoogleConfig, *, service: str = SERVICE
) -> Result[str, External]:
    """A fresh access token from the stored refresh token."""
    sent = await send(
        client,
        "POST",
        TOKEN_URL,
        what="token mint",
        service=service,
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": cfg.refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if isinstance(sent, Err):
        return sent
    if sent.value.status_code != 200:
        return failed("token mint", f"HTTP {sent.value.status_code}", service=service)
    return Ok(sent.value.json()["access_token"])


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
