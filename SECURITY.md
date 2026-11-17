# Security

turtlebot is a single-user tool that runs on your own machine against an IBKR **paper** account.
It has no user accounts, so there are no passwords to store or hash.

## Secrets

* Configuration lives in `.env`, which is git-ignored; `.env.example` contains no values.
* The optional Anthropic API key is held as a `SecretStr` and masked in `turtlebot config`.
* The broker connection needs no credentials in the code: TWS handles the login, and the bot only
  connects to the paper port 7497 and to account ids with the paper prefix.

## Dashboard

* Binds to `127.0.0.1` by default and has no write endpoints towards the broker or the database.
* Answers only to the host names in `TURTLE_DASHBOARD__ALLOWED_HOSTS` (DNS-rebinding guard) and
  accepts WebSocket connections only from its own origin.
* Sends a Content-Security-Policy plus `X-Frame-Options`, `X-Content-Type-Options` and
  `Referrer-Policy` on every response.
* The assistant runs the local Claude Code CLI with read-only tools. It is rate limited
  (`TURTLE_DASHBOARD__CHAT_MAX_PER_MINUTE`), answers one question at a time, and
  `turtlebot dashboard` refuses to start on a non-loopback address while it is enabled.
* The public demo on GitHub Pages is a static snapshot with synthetic data and no backend.

## Database

The SQLite state store is a local file under `var/` (git-ignored). All queries use bound
parameters.

## Reporting

Please report a vulnerability through GitHub's private vulnerability reporting on this repository
rather than in a public issue.
