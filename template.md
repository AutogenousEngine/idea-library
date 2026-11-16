---
title: "Idea Library"
description: "Turn your own writing and recordings into a map of your ideas: each idea backed by your own words, sorted into chapters drawn from your material, and connected, shown as a graph, a map and a reading page."
thumbnail: "template.svg"
version: v1
format: v2
---

# Idea Library

This file is the manifest for the **Idea Library** template (slug:
`idea-library`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

Turn your own writing and recordings into a map of your ideas: each idea backed by your own words, sorted into chapters drawn from your material, and connected, shown as a graph, a map and a reading page.

It turns a person's own writing and recordings into a library of their ideas.
Each idea carries short quotes in the author's own words, checked word for word
against the source, with a link back to where it came from. The ideas are sorted
into chapters named from the material itself and linked to each other. Links the
author actually stated ("expressed") are kept apart from ones the model only
suggests. The owner then browses it in the **Idea Library** app, which has four
views: a reading page (chapters, threads, sources), an interactive graph, an
idea map with labelled, explained connections and playable source audio or
transcripts, and an older "classic" searchable list. The owner's name, the
collection title, the chapters and the reading intro all come from the library
file, so nothing about the original author is built in. A new adopter starts
with an empty library and fills it by running the `build-idea-library` skill on
their own material: files, a Substack, or recordings from Plaud, Otter.ai or
voice notes.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/idea-library/`
- `system/supervisord.conf.d/idea-library.conf`
- `.agents/skills/build-idea-library/`

- `system/apps/idea-library/` -- the **Idea Library** app, a Flask package
  (`idea_library.runner`, console script `idea-library`). It serves the four pages
  from `src/idea_library/assets/` (`textbook.html` at `/`, `graph.html`,
  `map.html`, `classic.html`) and serves the user's data files read-only from
  `data/.apps/idea-library/` (override: `IDEA_LIBRARY_DATA_DIR`) at the same names
  the pages fetch: `map-data.json` (required), `sample.json`, `corpus.html`,
  `map-assets/`. Every chapter lives in `map-data.json`. It also serves `/api/schema` and
  `/health`. Its `testdata/` holds a small made-up library its tests use, and its
  tests live beside the code (`routes_test.py`, `runner_test.py`, browser tests in
  `pages_browser_test.py`).
- `system/supervisord.conf.d/idea-library.conf` -- the supervisord program
  `idea-library`. It registers the app with `system/scripts/forward_port.py`
  (`--manifest system/apps/idea-library/app.toml --url http://localhost:8093`) and
  then execs the app on port **8093** (loopback; override with
  `IDEA_LIBRARY_PORT` / `IDEA_LIBRARY_HOST`). `app.toml` sets
  `stop_when_no_windows = true`, so the shell stops it a minute after its last window
  closes and starts it again on the next request.
- `.agents/skills/build-idea-library/` -- the skill that builds or extends the
  library. `scripts/run.py` (a uv script) runs it one step at a time: `fetch-substack`,
  `fetch-plaud`, `fetch-otter`, `transcribe` (local faster-whisper), `ingest`,
  `estimate`, `extract`, `organize`, `connect`, `build`, `validate`, or `all`. It
  keeps its working store (raw sources with their origin links, cached model
  answers, settings, run reports, backups) in `data/.skills/build-idea-library/`,
  reads material from `data/idea-library-sources/`, and writes `map-data.json`,
  `sample.json`, `corpus.html` and `map-assets/` into the app's data folder.
  `references/library-format.md` is the data contract between the skill and the
  app. `assets/mcp-servers.json` carries the skill's own copies of the official
  Plaud and Otter.ai connector addresses, opened through the workspace's `mcpc`.

The skill and the app share only the data folder. The skill writes files and the
app reads them on each request, so a rebuilt library shows up on the next page
refresh with no restart.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


- requires_llm: the skill's extract, organize and connect steps call Claude through `library_model.make_complete`. With no API key it runs headless `claude -p` on the workspace's Claude sign-in (keyless, the default here). When the workspace has an `ANTHROPIC_API_KEY` (or a saved key snapshot from setup), it calls litellm directly instead, so either method works with no code change. The default model is `claude-sonnet-5-5` (`--model claude-haiku-5-5` is about 25 times cheaper). The skill shows a cost estimate before spending.

- **No library yet: run the skill first.** The template ships no library (the
  publisher's own library was deliberately left out). Until `build-idea-library`
  has run, every page opens and says "No library yet. Ask your agent to build your
  Idea Library from your writing and recordings." Ask the user for
  their name as it appears on recordings, what to keep private, and where their
  material is, then run the skill, show the estimate, and open the app on their
  own ideas.
- **Recording platform (optional).** Fetching from Plaud or Otter.ai needs the
  user to sign in once to that platform's official connector (the
  `connect-external-service` skill's official-connector route; addresses in the
  skill's `assets/mcp-servers.json`). Skip it if they have no recordings there.
  Other platforms work by exporting transcripts or audio into
  `data/idea-library-sources/`. The Otter.ai fetcher has only been tested against
  made-up results, so check its first real run.
- **Local transcription is untested on real audio.** `transcribe` downloads a
  faster-whisper model on first use and runs on CPU. It has not been run on real
  recordings yet, so check its output on the user's first batch.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

- poppler-utils: provides `pdftotext`, which the skill uses to read PDF sources.
  Without it, PDFs are set aside with a clear message and everything else still works.

Nothing else is installed system-wide. The skill's Python dependencies
(`pydantic`, `litellm`, and `faster-whisper` for transcription) are declared
inline in its uv scripts and fetched on first run. The Plaud and Otter.ai
fetchers use the `mcpc` client that ships with the stock workspace.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `connect-external-service` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-11-15) -- first publication: the Idea Library app, its supervisord program, and the build-idea-library skill, with no library data

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
