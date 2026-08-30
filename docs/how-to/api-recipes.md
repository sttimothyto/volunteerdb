# Use the JSON API

Recipes for scripts that talk to a live instance.

- The full endpoint list is in the [HTTP API reference](../reference/http-api.md).
- The exact request and response schemas are on the instance itself, at
  `/docs`.
- The examples below assume a local dev instance (`localhost:8080`) and `jq`.
- For production, substitute `https://vdb.example.org`.

## Get a token

```sh
TOKEN=$(curl -s localhost:8080/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@example.org","password":"demo"}' | jq -r .token)
```

- The account needs a password. OTP-only accounts cannot get tokens.
- Each login issues a fresh token and revokes the previous one. Store the
  token; do not log in for each request.
- 5 failures per email in 15 minutes trip the throttle.

Confirm the identity behind the token:

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

Responses redact what the token's account cannot see. The API uses the same
[permission matrix](../reference/permissions.md#permission-matrix) as the
GUI.

## Time travel

Most GETs accept `as_of` (ISO 8601; a naive value is server-local):

```sh
curl -s -H "Authorization: Bearer $TOKEN" \
  'localhost:8080/api/teams/3/roster?as_of=2026-01-01T00:00:00%2B02:00'
```

URL-encode a `+` in a timezone offset as `%2B`.

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

Membership creation upserts on (volunteer, team). If you post a different
role for a pair that exists, the role changes.

## Export and import

```sh
curl -s -H "Authorization: Bearer $TOKEN" -o parish.csv \
  localhost:8080/api/export/parish.csv
curl -s -H "Authorization: Bearer $TOKEN" -F file=@parish.csv \
  'localhost:8080/api/import?dry_run=true' | jq
```

Always inspect the dry-run report before you repeat the command without
`dry_run`.

## When it fails

| Status | Likely cause |
|---|---|
| 401 | Missing or expired token. Log in again. |
| 403 | The account lacks rights for that team or action. |
| 404 | Wrong id, or the entity did not exist at your `as_of`. |
| 409 | Uniqueness conflict (for example a duplicate team name). |
| 422 | Malformed body or query parameter. |
| 429 | Login throttled. Wait out the 15-minute window. |

Full contract: [HTTP API reference](../reference/http-api.md#error-contract).
