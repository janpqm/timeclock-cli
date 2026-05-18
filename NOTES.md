# API reverse-engineering notes

Working notes on the timeclock365 web flow. Fill these in as you capture requests from DevTools (Network tab → right-click → Copy as cURL, or export HAR).

## Base URL

`https://live.timeclock365.com`

## 1. Login

- URL: `POST /<path>`
- Form fields: `username=…&password=…&…`
- Anti-CSRF: ?
- Response: sets cookie `<name>=<value>`; redirects to `/<dashboard>`

```
# paste curl here
```

## 2. Punch in

- URL: `POST /<path>`
- Body: `{ location: "office", … }`
- Required headers / cookies: ?

```
# paste curl here
```

## 3. Punch out

- URL: `POST /<path>`

```
# paste curl here
```

## 4. List recent entries

- URL: `GET /<path>?from=YYYY-MM-DD&to=YYYY-MM-DD`
- Response shape: ?

```
# paste curl here
```

## 5. Status / "am I clocked in?"

Possibly derivable from #4, or there may be a dedicated endpoint shown on the dashboard.

```
# paste curl here
```

## Open questions

- Does the site rotate CSRF tokens per session or per page load?
- What is the response when the session has expired? (302 to login? 401 JSON?)
- Are locations free-text or do they come from a dropdown of pre-defined options?
- Is there a single "status" endpoint, or do we have to derive status from the latest entry?
