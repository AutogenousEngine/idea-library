# The library file (`map-data.json`)

The Idea Library app's pages read this file. The app's own sample,
`system/apps/idea-library/testdata/map-data.json`, is the contract, and the app's README
lists the optional fields the pages read. `validate` accepts all of them, and this skill
writes nothing else.

```json
{
  "title": "Idea Library",
  "author": "Robin",
  "collection": "Notes from the Garden Gate",
  "reading_intro": "Start with care, end with what we share.",
  "scope": "42 ideas and 51 connections drawn from 12 sources (...).",
  "disclaimer": "...",
  "coverage": {"scope_generated": true, "sources_ingested": 14, "sources_in_library": 12, "ideas": 42, "connections": 51, "last_built": "2026-11-14T16:00:00+00:00"},
  "chapters": [{"name": "Care and restraint", "subtitle": "why less intervention helps"}],
  "nodes": [{"id": "attention-becomes-interference", "title": "...", "summary": "...", "group": "Care and restraint",
             "x": 160, "y": 150, "writing_seed": "...",
             "evidence": [{"source_id": "the-slow-hive", "quote": "..."},
                          {"source_id": "plaud-0a1b2c3d4e5f", "quote": "...", "start_ms": 26500, "timestamp": "0:26"}]}],
  "edges": [{"id": "c-1", "source": "<idea id>", "target": "<idea id>", "label": "leads to",
             "basis": "expressed", "why": "...", "evidence": [{"source_id": "...", "quote": "..."}]}],
  "sources": [{"id": "the-slow-hive", "title": "The Slow Hive", "kind": "Writing", "url": "https://...",
               "raw": "<the author's words>", "audio": null, "transcript": null,
               "transcription_note": "...", "date": "...", "link_label": "Read the essay"}]
}
```

## Fields

- **Library**:
  - **`title`** (required). **`scope`** -- "About this collection". It is regenerated each
    build only while `coverage.scope_generated` is true, so a hand-written scope stays.
  - **`disclaimer`** -- the footer. **`coverage`** -- free-form counts.
  - **Whose library it is** -- optional; the pages fall back to neutral wording without
    them. The build fills each one only while it is empty, so a hand edit stays; a flag
    replaces it.
    - **`author`** -- the owner's name, e.g. "Robin's collected thinking". It comes from
      `--display-name`, else the first `--author`.
    - **`collection`** -- the collection's name, shown in the map's wordmark. The organize
      step names it from the material; `--collection` replaces it.
    - **`reading_intro`** -- the reading page's intro line, written by the organize step.
  - **`chapters`** (optional) -- every chapter in reading order, with a subtitle, from the
    organize step (names and subtitles drawn from the material). Each idea's `group`
    names one. The pages colour and order chapters by this list; a group missing from it
    is added in the order it first appears in `nodes` and still gets its own colour. The
    build keeps nodes sorted by chapter, so the two orders agree.
- **Ideas (`nodes`)**:
  - required: `id`, `title`, `summary`, `group`, numeric `x` and `y` (map position), and
    a non-empty `evidence` list
  - optional: `writing_seed` (a content idea: text or null)
- **Evidence**: `source_id` (a source's `id`) and `quote`. Quotes from a transcript also
  carry `start_ms` and `timestamp` (`m:ss`).
- **Connections (`edges`)**:
  - `id`, `source` and `target` (idea ids), `label` ("source <label> target"), and a
    `why`
  - `basis` -- `expressed` (the author's words link them) or `suggested` (an
    interpretation)
  - `evidence` -- may be empty for a suggested link
- **Sources**:
  - `id`, `title`, `kind`, `url`, and `raw` (the text quotes are checked against)
  - `url` is the origin link, or for a source with none, its saved copy
    (`map-assets/sources/<id>.<ext>`)
  - optional: `audio` and `transcript` (paths under `map-assets/`), `transcription_note`,
    `date`, and `link_label`, which replaces the wording of the link to the original. This
    skill leaves `link_label` unset: the pages already say "View recording in Plaud",
    "Read on Substack" or "Open saved copy" from the source's `kind` and `url`.
  - For a recorded conversation, `raw` holds only the author's turns, each headed
    `[m:ss] Speaker`. The full transcript, every speaker included, is at `transcript`.

## The checks

**`validate` errors** -- any of these breaks a page:
- a missing or mistyped field
- a duplicate id
- a connection or quote pointing at an idea or source that is not there
- a `basis` other than the two allowed
- a malformed `chapters` entry
- `author`, `collection`, `reading_intro` or a source's `link_label` that is not text

**Warnings:**
- a quote not found word for word in its source's `raw` (an error with `--strict`)
- an expressed connection with no quote
- a group with no `chapters` entry

The app's sample library has made-up quotes that are not in its sources, so it passes
with warnings.

**`build` refuses to write** when there is any error, or when a quote of an idea it is
adding is not in its source. Quotes this skill adds always pass, because they are checked
when the model returns them.

## The classic view's file (`sample.json`)

The build writes it from the library when it is missing or was written by this skill
before (`"generated_by": "build-idea-library"`): every idea as a content idea with its
first quote and suitable formats, plus `author` and `collection` copied from the library
and `note` (the scope).
