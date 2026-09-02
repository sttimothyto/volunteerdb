"""The Env's impure parts, faked: a clock a test moves by hand, predictable
tokens, and a mailer that records instead of sending.

Shared by tests/conftest.py (the `env` fixture) and tests/ui_sim_main.py (the
simulated app), so a page's mail lands in SIM_MAILER.sent and a test reads
it back from the one place."""

import json
import re
import zlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote
from uuid import UUID

import httpx
import numpy as np


class FakeClock:
    """A clock a test moves by hand."""

    def __init__(self, at: datetime | None = None) -> None:
        self.at = at if at is not None else datetime.now(UTC)

    def now(self) -> datetime:
        return self.at

    def advance(self, **delta) -> None:
        self.at += timedelta(**delta)


class FakeRng:
    """Predictable tokens and codes, numbered in the order they were minted."""

    def __init__(self) -> None:
        self.n = 0

    def _next(self) -> int:
        self.n += 1
        return self.n

    def token(self) -> str:
        return f"token-{self._next():04d}"

    def otp_code(self) -> str:
        return f"{self._next():06d}"

    def uuid(self) -> UUID:
        return UUID(int=self._next())


class RecordingMailer:
    """Every message the app would have sent, as (to, subject, body)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to: str, subject: str, body: str) -> bool:
        self.sent.append((to, subject, body))
        return True


class FailingMailer(RecordingMailer):
    """Records, and reports every send as failed."""

    async def send(self, to: str, subject: str, body: str) -> bool:
        await super().send(to, subject, body)
        return False


class FakeHttp:
    """env.HttpClients whose every client is routed through a MockTransport
    (the test_gsheets.py idiom), recording the requests."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.seen: list[httpx.Request] = []

    def client(
        self, *, timeout: float = 10.0, follow_redirects: bool = False
    ) -> httpx.AsyncClient:
        def record(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return self.handler(request)

        return httpx.AsyncClient(
            transport=httpx.MockTransport(record),
            timeout=timeout,
            follow_redirects=follow_redirects,
        )


# The simulated app's mailer (tests/ui_sim_main.py builds its Env around it).
# One instance for the whole test process: user_simulation re-executes the
# main file per simulation, and a fixture needs a list whose identity survives
# that, so it can hand the test the very list the next page action fills.
class SwitchableMailer(RecordingMailer):
    """Records, and reports each send as the test says: `failing` makes every
    send fail until switched back. The simulated app's mailer is one of these
    so a page's "created, not emailed" path can be reached without a
    different app."""

    def __init__(self) -> None:
        super().__init__()
        self.failing = False

    async def send(self, to: str, subject: str, body: str) -> bool:
        await super().send(to, subject, body)
        return not self.failing


# One instance for the whole test process: user_simulation re-executes the
# main file per simulation, and a fixture needs a list whose identity survives
# that, so it can hand the test the very list the next page action fills.
SIM_MAILER = SwitchableMailer()


class FakeEmbedder:
    """manual_search.Embedder without a model: a hashed bag of words, rows
    normalised, so two texts that share words are close and two that share
    none are not. `synonyms` folds words together ({"spreadsheet": "sheet"}):
    the one thing a real embedding does that keywords cannot, in a form a
    test can predict. crc32 rather than hash(): the same buckets in every
    process, so a test that passes once passes always."""

    _WORD = re.compile(r"\w+")

    def __init__(self, dim: int = 32, synonyms: dict[str, str] | None = None):
        self.dim = dim
        self.synonyms = synonyms or {}
        self.calls: list[list[str]] = []

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        self.calls.append(list(texts))
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in self._WORD.findall(text.lower()):
                word = self.synonyms.get(word, word)
                out[row, zlib.crc32(word.encode()) % self.dim] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return out / norms


class OffsetClock:
    """The real clock, shifted by an amount a test sets.

    The simulated app (tests/ui_sim_main.py) cannot take a clock from the test
    that drives it, so it takes this one, kept in this module the way
    SIM_MAILER is, and a test moves it with `advance()`. It ticks rather than
    freezes because the pages it serves share the database with the DB's own
    `clock_timestamp()` (history rows, as-of reads), and a frozen instant
    hours behind the database would make a page disagree with its own
    history. `SIM_CLOCK.offset` is zeroed before every test (conftest)."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, **delta) -> None:
        self.offset += timedelta(**delta)

    def reset(self) -> None:
        self.offset = timedelta()


SIM_CLOCK = OffsetClock()


class RaisingSessions:
    """A session factory whose every use fails: the database boundary, gone.

    For the code that promises never to raise whatever the database does
    (env.QuotaCell). Shaped like async_sessionmaker for the two calls those
    paths make: `sessions()` and `sessions.begin()`."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or RuntimeError("db is gone")

    def __call__(self):
        raise self.exc

    def begin(self):
        raise self.exc


class FakeGoogleCalendar:
    """Google Calendar v3, in memory, behind FakeHttp: calendars, their ACL
    rules and their events, answering the handful of calls services.gcal
    makes with the JSON shapes it reads. The token endpoint is here too.

    What the job did is read back from `calendars[cid]["events"]`, the
    request log (`http.seen`) and the per-verb tallies; what Google will do
    next is set with `insert_status`, `problems` (extra ACL rules) and
    `delete_calendar()`. Nothing is patched: the job reaches this through
    env.http exactly as it reaches Google."""

    API = "https://www.googleapis.com/calendar/v3"

    def __init__(self) -> None:
        self.calendars: dict[str, dict] = {}
        self.created: list[str] = []  # ids of the calendars the app asked for
        self.next_calendar = 0
        self.next_event = 0
        self.insert_status: int | None = None  # a status to answer inserts with
        self.token_status = 200
        # ACL rules Google will show on any calendar it creates for the app,
        # beyond the owner: a writer somebody granted by hand, say
        self.problem_rules: list[dict] = []
        self.http = FakeHttp(self.handle)

    # --- knobs ---------------------------------------------------------------

    def create(self, calendar_id: str | None = None, *, acl=None) -> str:
        """A calendar Google already has, as the parish account would see it."""
        cid = calendar_id or self._new_calendar_id()
        self.calendars[cid] = {
            "summary": cid,
            "acl": list(
                acl
                if acl is not None
                else [
                    {
                        "id": "user:admin@example.org",
                        "role": "owner",
                        "scope": {"type": "user", "value": "admin@example.org"},
                    },
                    {"id": "default", "role": "reader", "scope": {"type": "default"}},
                ]
            ),
            "events": {},
        }
        return cid

    def delete_calendar(self, calendar_id: str) -> None:
        """Somebody deleted it by hand in Google."""
        self.calendars.pop(calendar_id, None)

    def add_event(self, calendar_id: str, item: dict) -> str:
        gid = item.get("id") or self._new_event_id()
        self.calendars[calendar_id]["events"][gid] = {**item, "id": gid}
        return gid

    def requests(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.http.seen if self._is(r, method, suffix)]

    # --- the wire ------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        path = url.split("?", 1)[0]
        if path == "https://oauth2.googleapis.com/token":
            return httpx.Response(self.token_status, json={"access_token": "tok"})
        if not path.startswith(self.API):
            return httpx.Response(404, json={"error": "not google"})
        rest = path[len(self.API) :]
        if rest == "/calendars" and method == "POST":
            cid = self._new_calendar_id()
            body = json.loads(request.content)
            owner = {
                "id": "user:admin@example.org",
                "role": "owner",
                "scope": {"type": "user", "value": "admin@example.org"},
            }
            self.create(cid, acl=[owner, *self.problem_rules])
            self.calendars[cid]["summary"] = body.get("summary", "")
            self.created.append(cid)
            return httpx.Response(200, json={"id": cid})
        parts = rest.strip("/").split("/")
        # /calendars/{cid}[/acl[/{rule}] | /events[/{gid}]]
        if len(parts) < 2 or parts[0] != "calendars":
            return httpx.Response(404, json={})
        cid = unquote(parts[1])
        cal = self.calendars.get(cid)
        if cal is None:
            return httpx.Response(404, json={"error": {"code": 404}})
        tail = parts[2:]
        if not tail:
            return httpx.Response(200, json={"id": cid, "summary": cal["summary"]})
        if tail[0] == "acl":
            return self._acl(request, cal, tail[1:])
        if tail[0] == "events":
            return self._events(request, cal, tail[1:])
        return httpx.Response(404, json={})

    def _acl(self, request, cal, rest) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": cal["acl"]})
        if request.method == "POST":
            rule = {**json.loads(request.content), "id": "default"}
            cal["acl"].append(rule)
            return httpx.Response(200, json=rule)
        if request.method == "PATCH" and rest:
            rule_id = unquote(rest[0])
            for rule in cal["acl"]:
                if rule.get("id") == rule_id:
                    rule.update(json.loads(request.content))
                    return httpx.Response(200, json=rule)
            return httpx.Response(404, json={})
        return httpx.Response(405, json={})

    def _events(self, request, cal, rest) -> httpx.Response:
        events = cal["events"]
        if request.method == "GET" and not rest:
            params = request.url.params
            items = list(events.values())
            if params.get("privateExtendedProperty") == "vdb_managed=1":
                items = [
                    i
                    for i in items
                    if i.get("extendedProperties", {})
                    .get("private", {})
                    .get("vdb_managed")
                    == "1"
                ]
            return httpx.Response(200, json={"items": items})
        if request.method == "POST" and not rest:
            if self.insert_status is not None:
                return httpx.Response(self.insert_status, json={})
            gid = self.add_event_body(cal, json.loads(request.content))
            return httpx.Response(200, json={"id": gid})
        if rest:
            gid = unquote(rest[0])
            if request.method == "PATCH":
                if gid not in events:
                    return httpx.Response(404, json={})
                events[gid].update(json.loads(request.content))
                return httpx.Response(200, json=events[gid])
            if request.method == "DELETE":
                return httpx.Response(204 if events.pop(gid, None) else 410)
        return httpx.Response(405, json={})

    def add_event_body(self, cal, body: dict) -> str:
        gid = self._new_event_id()
        cal["events"][gid] = {**body, "id": gid}
        return gid

    def _new_calendar_id(self) -> str:
        self.next_calendar += 1
        return f"cal{self.next_calendar}@group.calendar.google.com"

    def _new_event_id(self) -> str:
        self.next_event += 1
        return f"g{self.next_event}"

    def _is(self, request: httpx.Request, method: str, suffix: str) -> bool:
        return request.method == method and str(request.url).split("?")[0].endswith(
            suffix
        )
