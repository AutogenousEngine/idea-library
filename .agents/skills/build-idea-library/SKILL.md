---
name: build-idea-library
description: Build or extend the user's Idea Library from their own material -- writing (articles, notes, markdown, PDFs, a Substack) and recordings (Plaud, Otter.ai, voice notes, captions). Finds the ideas the author actually expresses, each backed by quotes checked word for word against the source, sorts them into chapters drawn from the material, connects them (links the author stated kept apart from suggested ones), and writes the library the Idea Library app shows. Use when the user says "build my idea library from these files", adds new material to their library, or wants their writing or recordings turned into a map of ideas.
metadata:
  crystallized: true
  optional-connections:
    - service: Plaud
      connector: https://mcp.plaud.ai/mcp
      note: the user's recording platform, if they use Plaud -- an official connector they sign in to once; transcripts are fetched through it
    - service: Otter.ai
      connector: https://mcp.otter.ai/mcp
      note: the user's recording platform, if they use Otter.ai -- an official connector they sign in to once; transcripts are fetched through it
---

# Build an idea library

Turns a person's own material into the data the Idea Library app displays: ideas with
quotes in their own words, chapters, and labelled connections. Every quote is checked
word for word against the stored source before it is kept. Links the author stated
("expressed") stay distinct from the links the model only suggests. Re-running with more
material extends the library. It never replaces what is already there.

All commands run from the repo root:

```bash
uv run .agents/skills/build-idea-library/scripts/run.py <step> [options]
```

## Where things live

- **Sources folder** -- `data/idea-library-sources/` by default (`--sources` to change it;
  remembered). The user can drop files here. Fetchers write into subfolders of it
  (`substack/`, `plaud/`, `otter/`).
- **The library** -- the app's data folder `data/.apps/idea-library/`:
  - `map-data.json`, the library itself
  - `map-assets/`, holding saved copies of sources with no web link, full transcripts,
    and audio
  - `sample.json`, for the classic view
  - `corpus.html`, a searchable page of every source
  The last two are written only when missing or when this skill wrote them before, so a
  hand-made one is never replaced. `references/library-format.md` is the data contract,
  including the optional fields that name whose library it is: `author`, `collection`,
  `reading_intro` and `chapters`.
- **Working store** -- `data/.skills/build-idea-library/` holds:
  - every ingested source, as its raw text and origin link
  - cached model answers, so the same text is never paid for twice
  - the remembered settings
  - the last 20 run reports and the last 5 backups of the library
  A source's cached answers are evicted when its text changes.

Readable sources:
- writing: `.md`, `.txt` (front matter `title:`, `url:`, `kind:`, `date:` honoured), `.html`, `.pdf` (text PDFs; needs `pdftotext`)
- captions: `.srt`, `.vtt`
- transcript JSON from the fetchers or from Plaud, Otter or whisper exports, a Substack post, or a plain `{title, url, text}`
- Otter's `.txt` export
- audio (`.mp3`, `.m4a`, `.wav`, ...), once transcribed

A `<file>.source.json` sidecar can set `title`, `url`, `kind` and `date` for any file.

## Steps

### 1. [prose] Gather the material and the facts only the user knows

Ask, in one message:

- **Their name as it appears on recordings.** Speaker labels in transcripts decide whose
  words count; other speakers are kept in the full transcript but never quoted as theirs.
- **Anything private to keep out.** For example "anything in `private/`" or "recordings
  titled 'therapy'".
- **Where the material is**:
  - **Files on their computer** -- bring them into the sources folder with the
    `file-sharing` skill.
  - **A Substack** -- the publication's name or address.
  - **Recordings** -- ask which recording platform they use, then get access. Never ask
    for a password, and never store one:
    - **Plaud or Otter.ai.** Each has an official connector that the user signs in to
      once. Follow the `connect-external-service` skill. This is its official-connector
      row: `references/mcp.md` there covers the one-time sign-in in a browser the user
      takes over. The connector addresses are in this skill's `assets/mcp-servers.json`;
      `mcpc connect .agents/skills/build-idea-library/assets/mcp-servers.json:plaud @plaud`
      (or `:otter @otter`) opens the session once signed in. The fetchers also open it
      themselves, and say plainly when a sign-in is still needed.
    - **Any other platform** (Fireflies, Rev, Descript, voice memos, ...) -- ask the user
      to export transcripts (text, `.srt`/`.vtt`, or JSON) or the audio into the sources
      folder, through the `file-sharing` skill when they are on their computer.

### 2. [script] Fetch

Run only the fetchers that apply. Each writes one file per item and skips what is
already saved, so re-running fetches only what is new.

```bash
uv run .agents/skills/build-idea-library/scripts/run.py fetch-substack <name-or-address>
uv run .agents/skills/build-idea-library/scripts/run.py fetch-plaud [--since YYYY-MM-DD] [--exclude-title 'therapy|client'] [--audio]
uv run .agents/skills/build-idea-library/scripts/run.py fetch-otter [--since YYYY-MM-DD] [--query ...] [--exclude-title ...]
```

- **Substack**: posts that come back cut short (paid posts) are listed. A signed-in
  export is the way to get those in full.
- **Plaud `--audio`**: saves the audio of recordings that have no transcript, for step 3.
- **Endpoints and file layouts**: `references/recording-platforms.md`.

### 3. [script] Transcribe (only when there is audio without a transcript)

```bash
uv run .agents/skills/build-idea-library/scripts/run.py transcribe [--whisper-model base.en]
```

Runs whisper on this machine. The first run downloads the speech model. Expect roughly
real time on CPU with `base.en`.

### 4. [script] Ingest

```bash
uv run .agents/skills/build-idea-library/scripts/run.py ingest --author "Their Name" [--author "Alias"] [--exclude 'private/*']
```

It reports what was set aside and why:
- an excluded file
- an unsupported type
- audio not yet transcribed
- a conversation where the author never speaks
- a conversation with several speakers and no author named

Authors, exclusions and the folder are remembered for later runs.

### 5. [script] Estimate -- show it before spending

```bash
uv run .agents/skills/build-idea-library/scripts/run.py estimate
```

Tell the user the projected cost and time on the default model, `claude-sonnet-5-5`.
- Under about $5, go on.
- Over that, ask first; they may prefer `--model claude-haiku-5-5` (about 25 times
  cheaper, a little coarser).

For unattended runs, `all --max-cost <dollars>` stops before spending when the estimate
is higher.

### 6. [ai-script] Find ideas, sort them, connect them, then [script] build

```bash
uv run .agents/skills/build-idea-library/scripts/run.py extract
uv run .agents/skills/build-idea-library/scripts/run.py organize
uv run .agents/skills/build-idea-library/scripts/run.py connect
uv run .agents/skills/build-idea-library/scripts/run.py build [--title "..."] [--display-name "..."] [--collection "..."]
```

- **extract** reads each new or changed source.
  - It returns ideas with a summary, quotes, an essay seed and suitable formats.
  - A quote that differs from the source only in quote marks, dashes or spacing is
    restored to the source's exact wording. Any other quote is dropped, and an idea left
    with no quote goes too.
  - It runs 6 sources at a time (`--workers`). A source that fails is reported and
    retried on the next run.
- **organize** places new ideas into chapters. The chapter names come from the material.
  Existing chapters keep their names, and true duplicates merge into the idea they repeat.
  It also names the collection and writes the reading page's intro line.
- **connect** proposes links, first within each source (with its text) and then across
  the library. A link stays "expressed" only with a quote that passes the same check.
- **build** merges into the library:
  - What is already there keeps its fields, hand edits included.
  - Quotes of a source whose text changed are dropped if they no longer appear (and
    reported).
  - Ideas are laid out by chapter, and the result is checked against the contract.
  - The library names its owner (`--display-name`, else the author's speaker name), its
    collection, its reading intro and its chapters, so a new library shows the person's
    own name and chapters with no hand edits. Each of these is filled only while empty,
    so a hand edit stays; `--display-name` and `--collection` replace them.
  - The library is backed up before it is written in one step. If the check fails,
    nothing is written.

`run.py all` chains steps 4 and 6 (with the estimate) for a headless or scheduled run.
In chat, drive the steps one at a time so the user sees each one land.

### 7. [prose] Show it

Open the Idea Library app, or refresh it if it is already open (the `manage-desktop`
skill). Tell the user:
- the numbers, and the chapters it found
- anything set aside, and how many quotes were dropped
- that suggested connections are interpretations

Do not paste transcript content into chat beyond what they ask to see.

## Checking a library

```bash
uv run .agents/skills/build-idea-library/scripts/run.py validate [path/to/map-data.json] [--strict]
```

Exits 1 on anything that breaks the pages: missing fields, a dangling link, a duplicate
id. A quote missing from its source is a warning, or an error with `--strict`. The app's
own sample library passes.

## Adding material later

Put new files in the sources folder, or run the fetchers again, then run `all`. Unchanged
sources cost nothing. Only new or changed text reaches the model.

## Gotchas

- **Never edit the Idea Library app's pages from this skill.** It writes data only.
- **Never write or remove the app's separate extra-chapter file** (the app README names
  it). New chapters go into `map-data.json`.
- **A file that moves or is renamed counts as a new source.** Its old entry stays in the
  library.
- **A source deleted from the folder stays in the library** (it is curated work). Remove
  its ideas by hand if they should go.
- **Transcripts are the user's private words.** The scripts print titles and counts,
  never content, and so should you.
