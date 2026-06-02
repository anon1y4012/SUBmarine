# SUBmarine
A scanner for Plex overlap with streaming services

## Security

SUBmarine is intended for a trusted network. Its settings, connection-test, discovery,
debug, and sync endpoints do not require authentication. Do not expose the container
directly to the internet. Bind the published port to `127.0.0.1` for local-only use,
or put it behind an authenticated reverse proxy when remote access is needed.

The `/data` volume stores service credentials in the SQLite database. Restrict access
to that volume and to application logs.
