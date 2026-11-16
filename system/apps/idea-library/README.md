# idea-library

Browse the idea library: ideas pulled from voice notes and writing, shown as a searchable list and a map.

The pages are code and ship in `src/idea_library/assets/`: the reading page (`textbook.html`, also served
at `/` and `/index.html`), the idea map (`map.html`) and the classic view (`classic.html`). The runner adds
the shell page script to each and serves it under a Content-Security-Policy that admits its inline scripts
by hash, computed from the page itself, so an edited page needs no policy change.

The data is the user's and lives in the app's data folder (`data/.apps/idea-library/`, or
`IDEA_LIBRARY_DATA_DIR`), served at the same names the pages fetch:

- `map-data.json` -- the ideas, connections and sources (required; without it the reading page says it
  could not load)
  - optional `author`, `collection`, `reading_intro` and `chapters` (name and subtitle, in reading order)
    name whose library it is and its chapters; the pages colour chapters by that order and fall back to
    the order ideas first appear. A source's optional `link_label` names its original link.
- `sample.json` -- the classic view's sample (optional; may carry `author`, `collection` and `note`)
- `map-assets/` -- voice-note audio and full transcripts the map links to (optional)
- `corpus.html` -- the generated transcript-corpus page the map links to (optional)

The ideas quote automatic transcripts: never log their content.
