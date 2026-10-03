# Release Events v1

Release Bot accepts qualified software/model events at `POST /v1/releases` and dedupes globally by producer event id. Producers authenticate with `RELEASE_EVENTS_TOKEN`. Seen ids persist at `/data/release-events-seen.json`.

The existing npm/GitHub poller remains as a fallback during migration.
