# Recording platforms and other fetchers

What each fetcher calls, what comes back, and what it saves. Readers stay tolerant: every
layout below, and each platform's own export, reads into the same source record
(`scripts/library_sources.py`).

## How access works

Plaud and Otter.ai each publish an official hosted connector (an MCP server) that signs
in with the user's own account through OAuth. That is the `connect-external-service`
skill's official-connector row. No password, key or token is handled here:

1. The sign-in: `mcpc login <connector url>` in the background, then a browser the user
   takes over (`connect-external-service` -> `references/mcp.md`, "An OAuth sign-in").
2. The session: `mcpc connect .agents/skills/build-idea-library/assets/mcp-servers.json:<name> @<name>`.
   `fetchers.Connector.ensure()` does this itself. When the sign-in is missing, it stops
   with a message naming the connector address.
3. Calls: `mcpc --json @<name> tools-call <tool> '<json arguments>'`. The result's
   `content[].text` holds the tool's answer. Plaud wraps its JSON in a sentence and an
   `<untrusted-user-data-... source="plaud-recording">` block, which `unwrap_tool_text`
   strips.

The connectors are declared in this skill's own `assets/mcp-servers.json`, not the
workspace's root one. A template that ships the skill then lists them as optional, and an
adopter is never asked to sign in to a platform they do not use.

Any other platform: ask the user to export into the sources folder:
- text, `[m:ss] Name: words` lines
- Otter-style `Name  0:05` blocks
- `.srt` / `.vtt`
- JSON with `segments`
- the audio, for `transcribe`

## Plaud (`https://mcp.plaud.ai/mcp`, session `@plaud`)

Tools (all read-only):

| Tool | Arguments | Returns |
|---|---|---|
| `list_files` | `page` (1-based), `page_size` (min 10), `date_from` / `date_to` (`YYYY-MM-DD`, server-side, exact), `query` (name substring; scans only the newest 500) | `{"type": "list", "data": [{"id": "of_<hex>", "name", "created_at", "serial_number", "start_at", "duration" (ms)}], "page", "page_size"}`. No total: page until a short page. |
| `get_transcript` | `file_id`, `block` (`transaction` default; `transaction_polish`, `outline`, `mark_memo`), `cursor`, `limit` (max 500) | `{"file_id", "block", "total", "offset", "limit", "returned", "next_cursor", "segments": [{"start_time" (ms), "end_time", "content", "speaker", "original_speaker", "segment_uid"}]}`. Follow `next_cursor` until `total` is reached. |
| `get_file` | `file_id` | `{"id", "name", "created_at", "serial_number", "start_at", "duration", "presigned_url" (audio, valid 24 h), "source_list": [...], "note_list": [...]}` |
| `get_note`, `get_current_user` | | not used |

`fetch-plaud` saves `plaud/plaud-<hex>.json`:

```json
{"platform": "plaud", "id": "of_<hex>", "title": "<name>", "url": "https://web.plaud.ai/file/<hex>",
 "date": "<start_at>", "duration_ms": 412000, "segments": [<get_transcript segments, all pages>],
 "file": {<get_file without presigned_url, source_list, note_list>}}
```

- The signed audio link is never stored: it grants access, and it expires.
- A recording with no transcript is listed. With `--audio` its audio is saved as
  `plaud-<hex>.mp3` for `transcribe`.
- Speaker labels are Plaud's. A label can carry a "(you)" suffix, which still matches
  `--author`.

## Otter.ai (`https://mcp.otter.ai/mcp`, session `@otter`)

Otter's connector (launched 2026) has three read-only tools: `search`, `fetch` and
`get_user_info`.
- `search` takes a query plus filters (attendee, dates, folder, channel, keywords).
- `fetch` returns one conversation's speaker-attributed, timestamped transcript, with
  summary and attendees.

The argument names were not published when this was written, so `fetch-otter` reads each
tool's input schema (`mcpc --json @otter tools-get <tool>`) at run time:

- **search**: the `query`-like property gets `--query`, or an empty string. A `from` /
  `after` / `start_date` / `since` property gets `--since`. Results are read from
  `results`, `meetings`, `conversations`, `speeches`, `items`, `data` or `matches`. Each
  id comes from `id`, `otid`, `speech_id`, `conversation_id` or `meeting_id`, or from an
  `otter.ai/u/<id>` URL.
- **fetch**: takes the first id-like property (or `url`, given `https://otter.ai/u/<id>`).
  The transcript is read from a list under `transcript`, `segments`, `transcripts` or
  `utterances`, whose entries have `speaker` / `speaker_name` / `{name}`,
  `start_offset` / `start` (ms, seconds or `m:ss`) and `text`. It can also be read from
  text in Otter's export layout (a `Name  0:05` line, then the words).

`fetch-otter` saves `otter/otter-<id>.json`: `{"platform": "otter", "id", "title", "url":
"https://otter.ai/u/<id>", "date", "segments" | "text", "raw": <the whole fetch result>}`.
The raw result is kept, so a change to how it is read never needs a re-fetch.

**Not yet exercised against a live Otter account.** It is tested against made-up
results in the shapes above. Look at the first real result (the `raw` field) and adjust
`otter_record` if it differs.

Otter's text export (`.txt`) can also be dropped into the sources folder directly.

## Substack (public, no sign-in)

- `GET https://<publication>/api/v1/archive?sort=new&offset=<n>&limit=50`, paging until an
  empty page. Each item has a `slug`.
- `GET https://<publication>/api/v1/posts/<slug>` returns `title`, `subtitle`,
  `canonical_url`, `post_date`, `audience` (`everyone`, `only_paid`, ...) and `body_html`.
- Saved as `substack/substack-<slug>.json`: `{"platform": "substack", "title", "subtitle",
  "url", "date", "audience", "body_html", "post": <the whole response>}`.
- A post whose `audience` is not `everyone` comes back cut short and is listed as
  `partial`. The user can export their posts from Substack's settings (Exports) into the
  sources folder to get them whole.

## whisper (`transcribe`)

`scripts/transcribe.py` (faster-whisper, CPU, int8) writes
`<audio file>.transcript.json`:

```json
{"platform": "whisper", "audio": "<audio file name>", "model": "faster-whisper/base.en", "language": "en",
 "duration_s": 24.1, "status": "automatic transcription",
 "segments": [{"start": 0.0, "end": 9.8, "text": "...", "avg_logprob": -0.21, "no_speech_prob": 0.01}], "text": "..."}
```

- The source becomes a voice note, linked to its audio.
- A recording with no speech gets a file with no segments, so it is not retried, and
  ingest sets it aside.
- The model is cached in `data/.state/models/faster-whisper/` with a `.nobackup` marker.
