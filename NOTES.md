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

## 5. List recent entries / period summary — `DetailedAttendanceReport`

```graphql
query DetailedAttendanceReport(
    $pageNumber: Int
    $pageSize: Int
    $reportSettings: InputReportSettings!
) {
    detailedAttendanceReport(
        pageNumber: $pageNumber
        pageSize: $pageSize
        reportSettings: $reportSettings
    ) {
        elements {
            startDate endDate timezone
            totalDaysWorked totalHours totalLackOfHours
            creditHours totalStandardHours totalAbsenceHours
            user { id fullName company { id name } }
            records {
                date weekDay totalSeconds standardSeconds isWorkDay
                allSessions {
                    id startDate endDate
                    activityLog { locationType }
                }
                allAbsenceTimes { absenceName seconds durationType }
            }
        }
    }
}
```

Variables — most of these are filter toggles we just leave at their captured
defaults. The dynamic bits are the timestamp window, the company id, and the
user id.

```json
{
    "pageNumber": 1, "pageSize": 1,
    "reportSettings": {
        "userPickMode": "PICK_SELECT",
        "startDateTs": 1778803200, "endDateTs": 1781481599,
        "companies": [10365], "users": [108697],
        "companyBranches": [], "groups": [],
        "absences": [], "contracts": [], "weekDays": [],
        "specifyPeriod": false, "filterByAbsences": false,
        "onlyAddedOrEditedHours": false, "onlyEmptyDays": false,
        "onlyExceptions": false, "onlyEmptyEntries": false,
        "onlyAutomaticPunchOut": false,
        "notApprovedByEmployee": false, "notApprovedByManager": false,
        "includeTerminated": true, "onlyTerminated": false,
        "showOnlyDaysWithoutStandardHours": false,
        "sortByUserFullName": false, "filterByWeekDays": false,
        "showBonusColumn": false, "showTravelDurationAndDistance": false
    }
}
```

The response wraps everything as `{ code: 200, data: { detailedAttendanceReport: { elements: [<one block>] } } }`. The single element contains period totals plus a `records[]` array, one entry per calendar day. Each record's `allSessions[]` lists the day's punch sessions; `allAbsenceTimes[]` lists absences (Vacation, Sick leave, …).

`startDateTs` / `endDateTs` are Unix timestamps. Use `companies` = [`user.company.id`] and `users` = [`user_id` from `/api/auth/check`]. We fetch and cache `company_id` via the small `MeAndCompany` query:

```graphql
query MeAndCompany {
    currentUserContext {
        user { id company { id } }
    }
}
```

## Open questions / TODO

- Does the access-token alone work without the `AUTH` cookie? (testing required — user expects yes.)
- Token refresh: the response includes `refresh_token` + `refresh_expires_at` (5 min after access expires). For a CLI we may just re-login when access expires — simpler than implementing refresh.
- Are `BC-Mode: swap` and `Use-Http-Code: 1` required for mutations, or just hints?
- Does timeclock365 throttle / rate-limit?
