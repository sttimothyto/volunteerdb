# Use the JSON API

Recipes for scripting against a running instance. The full endpoint list is
in the [HTTP API reference](../reference/http-api.md); exact request/response
schemas are on the instance itself at `/docs`. Examples below assume a local
dev instance (`localhost:8080`) and `jq`; substitute
`https://vdb.sttimothyto.org` for production.

## Get a token

```sh
TOKEN=$(curl -s localhost:8080/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@example.org","password":"demo"}' | jq -r .token)
```

The account needs a password — OTP-only accounts cannot obtain tokens. Each
login issues a fresh token and revokes the previous one, so store it rather
than logging in per request (5 failures per email in 15 minutes trips the
throttle). Confirm identity with:

```sh
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/auth/me
```

## Read data

```sh
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/teams
curl -s -H "Authorization: Bearer $TOKEN" 'localhost:8080/api/volunteers?q=alvarez'
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/volunteers/1/impact
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/reports/coverage
```

Responses redact what the token's account may not see — the same
[permission matrix](../reference/permissions.md#permission-matrix) as the GUI.

## Time travel

Most GETs accept `as_of` (ISO 8601; naive = server-local):

```sh
curl -s -H "Authorization: Bearer $TOKEN" \
  'localhost:8080/api/teams/3/roster?as_of=2026-01-01T00:00:00%2B02:00'
```

Remember to URL-encode `+` in timezone offsets as `%2B`.

## Write data

```sh
# create a volunteer (admin), then put them on a team as a core member
VID=$(curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"first_name":"Test","last_name":"Person","email":"test@example.org"}' \
  localhost:8080/api/volunteers | jq -r .id)
curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "{\"volunteer_id\":$VID,\"team_id\":3,\"role\":\"core\"}" \
  localhost:8080/api/memberships
```

Membership creation upserts on (volunteer, team): posting a different role
for an existing pair changes the role.

## Export and import

```sh
curl -s -H "Authorization: Bearer $TOKEN" -o parish.csv \
  localhost:8080/api/export/parish.csv
curl -s -H "Authorization: Bearer $TOKEN" -F file=@parish.csv \
  'localhost:8080/api/import?dry_run=true' | jq
```

Always inspect the dry-run report before repeating without `dry_run`.

## When it fails

| Status | Likely cause |
|---|---|
| 401 | Missing/expired token — log in again |
| 403 | The account lacks rights for that team/action |
| 404 | Wrong id, or the entity didn't exist at your `as_of` |
| 409 | Uniqueness conflict (e.g. duplicate team name) |
| 422 | Malformed body or query parameter |
| 429 | Login throttled — wait out the 15-minute window |

Full contract: [HTTP API reference](../reference/http-api.md#error-contract).
