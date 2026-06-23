# SUBmarine
A scanner for Plex overlap with streaming services

## Security

SUBmarine is intended for a trusted network. After first-run setup, settings,
connection-test, discovery, debug, sync, and removal endpoints can require a
SUBmarine access token. New installs generate this token during setup and keep it
in the browser's local storage; the server stores only a SHA-256 digest.

For existing installs, set `SUBMARINE_AUTH_TOKEN` before upgrading to enable
authentication immediately:

```yaml
environment:
  SUBMARINE_AUTH_TOKEN: "choose-a-long-random-token"
```

If no token is configured on an existing database, SUBmarine keeps the local-first
behavior to avoid locking you out. Add `SUBMARINE_AUTH_TOKEN` and restart when
you are ready to enforce authentication.

Do not expose the container directly to the internet. Bind the published port to
`127.0.0.1` for local-only use, or put it behind an authenticated reverse proxy
when remote access is needed.

The `/data` volume stores service credentials in the SQLite database. Restrict access
to that volume, browser profiles that hold the access token, and application logs.
