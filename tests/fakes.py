"""The Env's impure parts, faked: a clock a test moves by hand, predictable
tokens, and a mailer that records instead of sending.

Shared by tests/conftest.py (the `env` fixture) and tests/ui_sim_main.py (the
simulated app), so a page's mail lands in SIM_MAILER.sent whichever way it
was sent -- through the effect interpreter or, during the transition, the
mail.send_email shim -- and a test reads it back from the one place."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx


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

    def hex(self, n: int) -> str:
        return f"{self._next():0{2 * n}x}"[-2 * n :]


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
SIM_MAILER = RecordingMailer()
