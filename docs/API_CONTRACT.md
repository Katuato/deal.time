# Mini App API contract

The interface uses same-origin `/api/v1` by default. Set `apiBaseUrl` in `config.js` to a public API origin when the API is hosted separately. Serve the Mini App and API over HTTPS.

Every request carries `Authorization: tma <Telegram.WebApp.initData>`. The server must validate the Telegram Web App signature and `auth_date`, identify the user from the validated payload, and authorize every resource against that user. Never trust a client-provided Telegram user ID. OAuth client secrets, bot tokens, and database passwords belong only in server-side environment variables.

All JSON responses below use UTF-8. Event dates use `YYYY-MM-DD`; times use `HH:mm` in the user's configured timezone. Errors should return `{ "error": { "message": "Human-readable message" } }`.

## Endpoints

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| GET | `/me` | — | `{ "user": { "id": "…", "timezone": "Europe/Moscow" } }` |
| GET | `/events?from=YYYY-MM-DD&to=YYYY-MM-DD` | — | `{ "events": [Event] }` |
| POST | `/events/parse` | `{ "message": "…" }` | `{ "event": Event }`; parses the text, saves the resulting event, and returns its canonical representation |
| PATCH | `/events/{id}` | `{ "title", "date", "time", "end", "repeat" }` | `{ "event": Event }` |
| DELETE | `/events/{id}` | — | `204 No Content` |
| DELETE | `/events/{id}/series` | — | `204 No Content`; cancels the event series |
| GET | `/tasks` | — | `{ "tasks": [Task] }` |
| POST | `/tasks` | `{ "title", "date", "time" }` | `{ "task": Task }` |
| PATCH | `/tasks/{id}` | `{ "done": true }` | `{ "task": Task }` |
| DELETE | `/tasks/{id}` | — | `204 No Content` |
| GET | `/notifications` | — | `{ "notifications": [Notification], "enabled": true }` |
| PATCH | `/notifications/{id}` | `{ "unread": false }` | `{ "notification": Notification }` |
| PUT | `/notifications/settings` | `{ "enabled": true }` | `{ "enabled": true }` |
| GET | `/google/status` | — | `{ "connected": true }` |
| POST | `/google/connect` | — | `{ "authorization_url": "https://…" }` |
| DELETE | `/google/connection` | — | `204 No Content` |

`Event` fields: `id`, `title`, `date`, `time`, `end`, `repeat`, `source`, `sync`, `chat`, `status`; optional `html_link`, `googleUrl`, and `seriesId`. `Task` fields: `id`, `title`, `date`, `time`, `done`. `Notification` fields: `id`, `title`, `text`, `extra`, `kind`, `unread`.

The events query is scoped to the requested inclusive date range. The calendar requests one month at a time; the history screen displays the events currently loaded by that query. A later API revision can add cursor-based history pagination if the dataset grows beyond what one range can return.

The current bot branch has a Google OAuth callback but does not implement these Mini App JSON routes. This document defines the interface boundary; the Mini App reports API errors until the server implements it. Google OAuth must use the server-side web flow, configured HTTPS redirect URI, and server-held client credentials. Do not place Google credentials in this static frontend. The current UI supports one default Google Calendar and does not ask the user to choose among calendars.

## Demo

Append `?demo=1` to the Mini App URL to explicitly load the bundled sample data. In this mode data is local and Google/API actions are simulations. Without that parameter, the app starts in live mode and requires Telegram `initData` plus the API above.
