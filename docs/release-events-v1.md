# Release Events v1

The inbox accepts qualified software and model events at `POST /v1/releases` and delivers at most one Telegram message per caller `id` for the life of the ledger. The legacy npm/GitHub poller is unchanged and still owns OpenClaw, Hermes Agent, Codex, and Claude Code during migration.

Summaries and release notes are untrusted text. They are escaped, truncated, and sent. They are never executed and never written to the log.

This is not exactly-once delivery. See the crash window below.

## Endpoints

| Method | Path | Auth | Result |
|---|---|---|---|
| GET | `/health` | no | `200` body `ok`. Liveness only. It stays `200` when the ledger is corrupt or delivery is broken. |
| GET | `/ready` | no | `200` JSON when every flag is true, otherwise `503`. Body is exactly `{"token":bool,"telegram":bool,"state_writable":bool,"worker":true}`. Booleans only, never values. No Telegram call. `state_writable` is false when the state directory cannot be written or the ledger cannot be read. `worker` is true when this process answered. |
| POST | `/v1/releases` | `Authorization: Bearer $RELEASE_EVENTS_TOKEN` | See responses. |

`401` when the header is missing, wrong, or `RELEASE_EVENTS_TOKEN` is unset. Comparison uses `hmac.compare_digest`. The body is empty so the response does not say which of those three it was.

`Content-Type` must be `application/json` (`415` otherwise). `Content-Length` must be present and in `1..65536` (`400 bad length` when it is present but out of range). Missing length or `Transfer-Encoding` (chunked) is `411 length required`. Malformed JSON is `400 invalid json`. A JSON value that is not an object is `400 invalid body`.

## Validation

`validate` returns `""` or a short reason. The HTTP body of a `400` is that reason. Required fields `schema`, `id`, `kind`, `name`, `version`, `source`, and `url` must be non-empty strings after strip.

| Check | Reason |
|---|---|
| Field absent or blank | `missing <field>` |
| Field present but not a string, including JSON `null` | `invalid <field>` |
| `schema` other than `release-event/v1` | `unsupported schema` |
| `kind` other than `software` or `model` | `invalid kind` |
| `id` longer than 256 characters or not matching `^[A-Za-z0-9][A-Za-z0-9:._/@+-]*$` | `invalid id` |
| `name`, `version`, or `source` longer than 200 characters | `invalid <field>` |
| `url` longer than 2048, not `https`, empty host, any whitespace or control (including a trailing newline), or `"` / `'` | `invalid url` |
| `summary` present and not a string, or longer than 4000 characters | `invalid summary` |
| `metadata` present and not an object, or its JSON is over 8 KB | `invalid metadata` |

`http`, `javascript`, and `ftp` URLs are rejected. There is no hash fallback: a null, blank, or missing `id` is a `400`, not a derived id.

## Responses

| Status | Body | Meaning |
|---|---|---|
| 202 | `accepted` | This call sent the message and marked the id `delivered`. |
| 202 | `in_progress` | This id is already `pending` in this process. No second send. Not confirmation that Telegram accepted the message; retry later if you do not get `accepted` or `duplicate`. |
| 200 | `duplicate` | Already `delivered`, or `unknown` after a crash. Do not retry. |
| 200 | `owned_by_poller` | `metadata.repo` or `metadata.package` is on the legacy-owned list. No send, nothing recorded. |
| 503 | `delivery failed` | Telegram send returned false. State is `failed`. Retry. |
| 503 | `state unavailable` | Ledger missing-or-corrupt distinction failed closed, or the directory is not writable. The bad file is left in place. Retry later. |
| 409 | `gave_up` | Five failed attempts have been recorded. Later posts do not send. |
| 400 | reason | Fix the event. |
| 401 | empty | Fix the bearer token. |

The first four failed attempts return `503`. The fifth failed attempt returns `409 gave_up`. A further post returns `409` and does not send.

## Legacy-owned deny list

`RELEASE_EVENTS_LEGACY_OWNED` defaults to:

`openclaw/openclaw,nousresearch/hermes-agent,openai/codex,anthropics/claude-code,openclaw,hermes-agent,@openai/codex,@anthropic-ai/claude-code`

Match is case-insensitive on `metadata.repo` and `metadata.package`. An empty value disables the list. This is the receiver half of the migration partition: the poller still alerts those four products; a forwarded event for them is acknowledged and dropped.

## State

Two files, both on the volume:

| File | Env | Default | Contents |
|---|---|---|---|
| Legacy list | `RELEASE_EVENTS_STATE` | `/data/release-events-seen.json` | JSON list of ids. Migration source. Not rewritten into the v2 shape. |
| v2 ledger | `RELEASE_EVENTS_STATE_V2` | sibling `release-events-state.json` (default `/data/release-events-state.json`) | `{"version":2,"events":{id:{status,attempts,first_seen,last_attempt,delivered_at}}}` |

If both env values point at the same path, the v2 file is forced onto a different name so a ledger write cannot replace the legacy list.

Statuses are `pending`, `delivered`, `failed`, and `unknown`. `delivered_at` is an ISO timestamp or `null`.

On load, every id in the legacy list that is not already in v2 is copied in as `delivered`. Ids already in v2 keep their v2 status. The legacy file is not modified by that copy. A missing file is a fresh install. Invalid JSON, the wrong shape, or an unreadable file is `503 state unavailable` with log `[EVENTS][ERROR] state corrupt`. The bytes are not overwritten and are not renamed.

Each newly delivered id is also appended to the legacy list, capped at the newest 20,000, so a rollback to `f4b8e89` still treats those ids as seen. The poller's `last-version*.txt` files and their `{"npm","github_tag"}` shape are not used here.

Writes use a unique temp file in the same directory (`NamedTemporaryFile(..., delete=False)`), `fsync`, then `os.replace`. A process-wide lock covers load, decide, and persist. It is not held across the Telegram call. One id has one in-flight send: the winner writes `pending`, and a concurrent post gets `in_progress`.

Delivered rows are kept if they are newer than 90 days or among the newest 20,000 delivered ids, whichever keeps more. `pending`, `failed`, and `unknown` are never evicted.

## Remaining crash window

`pending` is written before the send. `delivered` is written only after `send_telegram` returns true.

If the process dies after Telegram accepts the message and before `delivered` is fsynced, the row is still `pending`. The next startup marks it `unknown`, logs `[EVENTS] id=<id> outcome=unknown`, and does not send it. A later POST of the same id returns `200 duplicate`. The two cases "send succeeded" and "process died before the send" are not distinguishable, so the row is not retried. That can drop an alert. It will not emit a second one.

A narrower window sits on the rollback path: v2 can already say `delivered` while the append to the legacy list has not happened. Rolling back to `f4b8e89` can alert that one id again. Fixing the legacy list does not cause another send on this version.

Startup does not auto-resend `pending` or `unknown`. Do not describe this inbox as exactly-once.

## Rendering

Name, version, summary, and the href are passed through `html.escape(..., quote=True)`, so `"`, `'`, `&`, `<`, and `>` cannot break the HTML. The href is still only reached for URLs that already passed validation. The message is shortened until it is at most 4096 characters without cutting an escape sequence or leaving an unclosed tag. `bot.send_telegram` still does its own HTML-then-plain retry. If that call returns false, the inbox tries one plain-text version (tags stripped, `&amp;` `&lt;` `&gt;` decoded) and does not try a third time.

## Process supervision

`runner.py` wires `bot` into the inbox, starts `serve()` on a daemon thread, and runs `bot.daemon()` on the main thread. The poller cadence, baselines, and silent first-run behavior are unchanged.

If the HTTP thread raises or returns, the process logs `[EVENTS][ERROR] http inbox thread died` and `os._exit(1)`. Railway `restartPolicyType` is already `ON_FAILURE`, so the platform restarts the whole process, poller included. A handler error (bad JSON, a failed send, a corrupt ledger) does not take the poller down.

## Logs

`[EVENTS] id=<id> outcome=received|rejected(<reason>)|duplicate|pending|delivered|failed|gave_up|owned_by_poller|unknown`

`id` is logged only when it matches the id rule; otherwise the field is `-`. Headers, tokens, and bodies are not logged. `pending` in the log is the `202 in_progress` response. `unknown` is startup-only.

## Rollback

Revert the commit on `main` (GitHub revert PR) so Railway builds the previous image, or redeploy deployment `5797e3b5` (that redeploy needs Sov's approval). No ledger edit is required: v2 lives in its own file, the legacy list is still a JSON list of ids, and ids delivered by this version were appended there. The poller's version files are untouched, so OpenClaw, Hermes, Codex, and Claude Code alerts are unchanged by the revert.

## After this is deployed

Producers stay inert until this revision is merged, the running Railway commit matches the merge, the log shows both `[START] release event inbox` and the poller checks, and `GET /ready` is `200`. A live test event waits for Sov's explicit approval, goes to a verified private destination, and is repeated once to prove the duplicate response.
