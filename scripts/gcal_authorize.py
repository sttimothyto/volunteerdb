#!/usr/bin/env python3
"""One-time Google Calendar authorization for the calendar sync.

Run this on a machine with a browser (never the server), signed in — in the
browser — as the parish Google account that owns the calendar:

    python scripts/gcal_authorize.py CLIENT_ID CLIENT_SECRET

It opens the consent URL, catches the redirect on a loopback port, exchanges
the code, and prints the refresh token to paste into VDB_GCAL_REFRESH_TOKEN
(docs/how-to/google-calendar-sync.md). Stdlib only, like the host-side
decorate-sheets script: no google client libraries anywhere in the project.

Scope is calendar.events only — the sync writes events on one calendar it is
told about; it cannot create calendars or touch anything else.
"""

import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/calendar.events"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PORT = 8765


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    client_id, client_secret = sys.argv[1], sys.argv[2]
    redirect = f"http://127.0.0.1:{PORT}/"
    state = secrets.token_urlsafe(16)
    consent = (
        AUTH_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": SCOPE,
                # offline + consent: force a NEW refresh token even if this
                # client was authorised before (Google omits it otherwise)
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
    )

    code: list[str] = []

    class Catcher(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's contract
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            body = "Something went wrong - check the terminal."
            if query.get("state") == [state] and query.get("code"):
                code.append(query["code"][0])
                body = "Authorized - you can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args) -> None:  # keep the terminal readable
            pass

    print("Opening the consent screen; sign in as the parish account.")
    print(f"If no browser opens, visit:\n\n  {consent}\n")
    webbrowser.open(consent)
    with http.server.HTTPServer(("127.0.0.1", PORT), Catcher) as server:
        while not code:
            server.handle_request()

    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code[0],
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        }
    ).encode()
    with urllib.request.urlopen(  # noqa: S310 - fixed https endpoint
        urllib.request.Request(TOKEN_URL, data=data)
    ) as resp:
        tokens = json.load(resp)

    refresh = tokens.get("refresh_token")
    if not refresh:
        print("No refresh token returned - revoke the app's access at")
        print("https://myaccount.google.com/permissions and run this again.")
        return 1
    print("\nVDB_GCAL_REFRESH_TOKEN (store it like the SMTP2GO key):\n")
    print(f"  {refresh}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
