# API reverse-engineering notes

Captured from Firefox HAR exports of the timeclock365 web UI (see `flows/`).

## Base URL

`https://live.timeclock365.com`

## 1. Login — two-step

### Step 1: `POST /api/auth/login`

```json
{
  "username": "user@example.com",
  "client_id": "ZWUyYzllNGUzYjlhZDAwNzBiYTgwN2ZjYjU2YWZiNDRhZDhiYTFkNmM5NTkzNzJlNDc1NzRkM2Y5OGFiODc0Nw"
}
```

The `client_id` is a per-app constant (identical across all our captures). Response:

```json
{
  "status": "additional_authentication_steps_required",
  "auth_token": "Yjc4NGM5Nj…",
  "expires_at": 1779113361,
  "auth_data": { "auth_type": "password" }
}
```

### Step 2: `POST /api/auth/check`

Send the password as `code`, with the `auth_token` from step 1 in an `Auth-Token` request header:

```
Auth-Token: Yjc4NGM5Nj…
Content-Type: application/json

{ "code": "<password>" }
```

Response:

```json
{
  "status": "authenticated",
  "access_token": "ZTE5NjhmZj…",
  "expires_at": 1779112783,
  "refresh_token": "ZWNhMWU1ZT…",
  "refresh_expires_at": 1779113083,
  "user_id": 108697
}
```

Access tokens are short-lived (~10 minutes). The server also sets an `AUTH` cookie with the same JSON, but the user-confirmed view is that **only `access-token` matters** for subsequent calls. We'll re-verify with live testing.

## 2. Authenticated requests

All subsequent calls go to `POST /graphql/` with these request headers:

```
Content-Type: application/json
access-token: <access_token>
App-Accept-Language: en
BC-Mode: swap          (sent on most queries — appears optional)
Use-Http-Code: 1       (sent on most queries — appears optional)
```

## 3. Punch in / punch out — same mutation, different `punchType`

```graphql
mutation makePunchDashboard($punch: InputWebPunch!) {
    makePunchByDashboard(punch: $punch) {
        id startDate endDate timezone locationType
    }
}
```

Variables:

```json
{ "punch": { "locationType": "OFFICE", "punchType": "PUNCH_IN" } }
{ "punch": { "locationType": "OFFICE", "punchType": "PUNCH_OUT" } }
```

`locationType` is one of: `OFFICE`, `HOME`, `ABROAD`, `FIELD`.

Response:

```json
{
  "code": 200,
  "data": {
    "makePunchByDashboard": {
      "id": 17575782,
      "startDate": "2026-05-19 05:38:39",
      "endDate": null,
      "timezone": "Europe/Berlin",
      "locationType": "OFFICE"
    }
  }
}
```

When punching out, the same object comes back with `endDate` populated.

## 4. Status — `DashboardWebPunchQuery`

```graphql
query DashboardWebPunchQuery {
    webPunch {
        ip
        locationTypes        # ["OFFICE","HOME","ABROAD","FIELD"]
        locationType         # current default
        session {
            id startDate endDate timezone locationType
        }
        lunchSession { id startDate endDate }
    }
}
```

- `session == null` → currently punched out
- `session != null && session.endDate == null` → currently punched in (since `session.startDate`)
- `lunchSession` works the same way for lunch breaks

## 5. List recent entries — NOT YET CAPTURED

We don't have a HAR for the timecard / history view. To finish `tc list`, capture a flow that shows multiple past punch sessions (probably the Timecard page, `/en/timecard` or similar). Expected operation name might be `MyTimesheetQuery` or similar; if so, paste its query and one response and we can wire it up.

## Open questions / TODO

- Does the access-token alone work without the `AUTH` cookie? (testing required — user expects yes.)
- Token refresh: the response includes `refresh_token` + `refresh_expires_at` (5 min after access expires). For a CLI we may just re-login when access expires — simpler than implementing refresh.
- Are `BC-Mode: swap` and `Use-Http-Code: 1` required for mutations, or just hints?
- Does timeclock365 throttle / rate-limit?
