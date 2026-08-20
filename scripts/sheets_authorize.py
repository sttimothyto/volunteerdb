#!/usr/bin/env python3
"""One-time Google authorization for the roster-spreadsheet sync.

Run this on a machine with a browser (never the server), signed in — in the
browser — as the parish Google account that owns the roster folder:

    python scripts/sheets_authorize.py CLIENT_ID CLIENT_SECRET [PORT]

Reuse the OAuth client the rclone backup remote already uses. That is not
laziness: drive.file is scoped per OAuth CLIENT per file, so only that client
can touch the roster sheets it created. A fresh client would be unable to
change their sharing, or to write them at all.

PORT is the loopback port the consent screen redirects to (default 8765). A
"Desktop app" client accepts any loopback port; a "Web application" one
accepts only the redirect URIs registered against it, and answers anything
else with redirect_uri_mismatch. rclone registers 53682, so pass that if the
default is refused.

It opens the consent URL, catches the redirect on a loopback port, exchanges
the code, and prints the refresh token to paste into VDB_SHEETS_REFRESH_TOKEN
(docs/how-to/roster-spreadsheets.md). Stdlib only, like scripts/gcal_authorize:
no google client libraries anywhere in the project.

Two scopes, and the split matters:

    spreadsheets  read and write the cells of any sheet this account can
                  reach. "Anyone with the link can edit" is what puts a
                  leader's own sheet in that set — it is the whole reason a
                  roster no longer has to live in a folder we own.
    drive.file    create a sheet in the roster folder and set its sharing,
                  limited to files this client itself created.

Deliberately NOT the full drive scope: nothing here needs to enumerate, read
or delete the account's other files, and the sync never opens a sheet it was
not handed the id of.
"""

import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

SCOPE = " ".join(
    (
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
    )
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PORT = 8765


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(__doc__)
        return 2
    client_id, client_secret = sys.argv[1], sys.argv[2]
    port = int(sys.argv[3]) if len(sys.argv) == 4 else PORT
    redirect = f"http://127.0.0.1:{port}/"
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
    with http.server.HTTPServer(("127.0.0.1", port), Catcher) as server:
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
    print("\nVDB_SHEETS_REFRESH_TOKEN (store it like the SMTP2GO key):\n")
    print(f"  {refresh}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
