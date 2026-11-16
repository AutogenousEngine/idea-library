# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2", "litellm>=1.88.1"]
# ///
"""Build or extend an Idea Library from a person's own writing and recordings.

    uv run .agents/skills/build-idea-library/scripts/run.py <step> [options]

Steps, in order (``all`` chains ingest, extract, organize, connect and build):

    fetch-substack URL   save a Substack publication's posts into the sources folder
    fetch-plaud          save Plaud transcripts (through the user's Plaud connector)
    fetch-otter          save Otter.ai transcripts (through the user's Otter connector)
    transcribe           transcribe audio that has no transcript (local whisper)
    ingest               read the sources folder into the working store
    estimate             projected cost and time of the model steps still to run
    extract              find the ideas in each new or changed source   [model]
    organize             sort new ideas into chapters, merge duplicates  [model]
    connect              connect new ideas to each other and the library [model]
    build                merge into the library, check it, write it
    validate [FILE]      check a library file against the app's data contract
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import library_build as lb  # noqa: E402
import library_model as lm  # noqa: E402
import library_sources as ls  # noqa: E402
from library_store import (  # noqa: E402
    DEFAULT_LIBRARY_DIR,
    DEFAULT_SOURCES_DIR,
    DEFAULT_STATE_DIR,
    Store,
    read_json,
    write_json,
    write_text,
)

LIBRARY_FILE = "map-data.json"
SAMPLE_FILE = "sample.json"
CORPUS_FILE = "corpus.html"
CORPUS_TEMPLATE = SCRIPTS.parent / "assets" / "corpus-template.html"
DEFAULT_WORKERS = 6
# Rough per-model prices (USD per million tokens, input / output) for the estimate only.
PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}
TOKENS_PER_WORD = 1.35
OUTPUT_TOKENS_PER_CALL = 1400
SECONDS_PER_CALL = 13


class StepError(RuntimeError):
    """A step that cannot run (no sources, nothing extracted yet); exits 2 with the message."""


class Context:
    def __init__(
        self, args: argparse.Namespace, complete: lm.Complete | None = None
    ) -> None:
        self.args = args
        self.store = Store(Path(args.state_dir))
        self.library_dir = Path(args.library_dir)
        self.library_file = self.library_dir / LIBRARY_FILE
        self.workers = max(
            1, int(getattr(args, "workers", DEFAULT_WORKERS) or DEFAULT_WORKERS)
        )
        self._complete = complete
        self.settings = self.store.settings()

    @property
    def model(self) -> str:
        return (
            getattr(self.args, "model", None)
            or self.settings.get("model")
            or lm.DEFAULT_MODEL
        )

    def complete(self) -> lm.Complete:
        if self._complete is None:
            self._complete = lm.make_complete(self.model)
        return self._complete

    def parallel(self, fn: Callable[[Any], Any], items: list[Any]) -> list[Any]:
        if len(items) <= 1 or self.workers == 1:
            return [fn(i) for i in items]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(fn, items))

    def sources_dir(self) -> Path:
        value = getattr(self.args, "sources", None) or self.settings.get("sources")
        return Path(value) if value else DEFAULT_SOURCES_DIR


def say(message: str) -> None:
    print(message, flush=True)


# ---- ingest -------------------------------------------------------------------


def step_ingest(ctx: Context) -> dict[str, Any]:
    args = ctx.args
    settings = ctx.settings
    if getattr(args, "author", None):
        settings["authors"] = args.author
    if getattr(args, "exclude", None):
        settings["excludes"] = sorted(
            set(settings.get("excludes", [])) | set(args.exclude)
        )
    root = ctx.sources_dir()
    settings["sources"] = str(root)
    if not root.is_dir():
        raise StepError(
            f"the sources folder {root} does not exist; put the material there or pass --sources"
        )
    authors = settings.get("authors", [])
    readable, skipped = ls.discover(root, settings.get("excludes", []))
    ids = ctx.store.source_ids_by_path()
    previous = ctx.store.all_sources()
    counts = {"new": 0, "changed": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    records = []
    for path in readable:
        try:
            records.append(ls.read_file(path, root))
        except (ls.SourceReadError, OSError, UnicodeError) as exc:
            skipped.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "reason": f"could not read: {exc}",
                }
            )
            counts["failed"] += 1
    library = load_library(ctx)
    ls.assign_ids(records, ids, {s["id"] for s in (library or {}).get("sources", [])})
    seen = set()
    for record in records:
        ls.attribute(record, authors)
        seen.add(record["id"])
        old = previous.get(record["id"])
        if old is None:
            counts["new"] += 1
        elif old.get("sha256") != record["sha256"] or old.get("raw") != record["raw"]:
            counts["changed"] += 1
        else:
            counts["unchanged"] += 1
        record["origin_file"] = str((root / record["path"]).resolve())
        if record["status"] != "ok":
            counts["skipped"] += 1
            skipped.append({"path": record["path"], "reason": record["skip_reason"]})
        ctx.store.save_source(record)
        ctx.store.evict_other_extractions(record, lm.PROMPT_VERSION)
    ctx.store.save_source_ids(ids)
    ctx.store.save_settings(settings)
    missing = sorted(set(previous) - seen)
    readable_count = len(records)
    if readable_count == 0 and not previous:
        raise StepError(f"no readable sources in {root}")
    summary = {
        **counts,
        "read": readable_count,
        "set_aside": skipped,
        "no_longer_in_folder": missing,
    }
    say(
        f"Read {readable_count} sources: {counts['new']} new, {counts['changed']} changed, {counts['unchanged']} unchanged; "
        f"{len(skipped)} set aside."
    )
    for item in skipped:
        say(f"  set aside {item['path']}: {item['reason']}")
    if missing:
        say(
            f"  {len(missing)} earlier sources are no longer in the folder; the library keeps them."
        )
    return summary


# ---- extract ------------------------------------------------------------------


def pending_sources(ctx: Context) -> list[dict[str, Any]]:
    return [
        s
        for s in ctx.store.all_sources().values()
        if s.get("status") == "ok"
        and ctx.store.load_extraction(s, lm.PROMPT_VERSION) is None
    ]


def step_estimate(ctx: Context) -> dict[str, Any]:
    pending = pending_sources(ctx)
    words = sum(len(s["raw"].split()) for s in pending)
    calls = sum(len(lm.chunk_text(s["raw"])) for s in pending)
    family = next((k for k in PRICES if k in ctx.model), "sonnet")
    price_in, price_out = PRICES[family]
    extract_cost = (
        words * TOKENS_PER_WORD * price_in + calls * OUTPUT_TOKENS_PER_CALL * price_out
    ) / 1e6
    # Connecting reads each new source again and the idea cards once per batch of new ideas.
    connect_cost = extract_cost * 1.3
    organize_cost = (
        calls * 300 * TOKENS_PER_WORD * price_in
        + OUTPUT_TOKENS_PER_CALL * 2 * price_out
    ) / 1e6
    total_calls = calls * 2 + 1
    seconds = total_calls * SECONDS_PER_CALL / ctx.workers + SECONDS_PER_CALL * 2
    result = {
        "sources": len(pending),
        "words": words,
        "model": ctx.model,
        "model_calls": total_calls if pending else 0,
        "estimated_cost_usd": round(extract_cost + connect_cost + organize_cost, 2)
        if pending
        else 0.0,
        "estimated_minutes": round(seconds / 60, 1) if pending else 0.0,
    }
    if pending:
        say(
            f"{len(pending)} sources ({words:,} words of the author's) wait for the model: about "
            f"${result['estimated_cost_usd']:.2f} and {result['estimated_minutes']} minutes on {ctx.model} "
            f"({result['model_calls']} calls, {ctx.workers} at a time). A rough estimate."
        )
    else:
        say("Nothing waits for the model: every readable source has been read.")
    return result


def step_extract(ctx: Context) -> dict[str, Any]:
    pending = pending_sources(ctx)
    if not pending:
        say("No new or changed sources to read.")
        return {"sources": 0, "ideas": 0, "cost_usd": 0.0, "failed": []}
    say(f"Finding ideas in {len(pending)} sources with {ctx.model} ...")
    complete = ctx.complete()

    def one(
        source: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        try:
            result = lm.extract_source(source, complete)
        except Exception as exc:  # one source failing must not sink the run
            return source, None, f"{type(exc).__name__}: {exc}"[:300]
        result["model"] = ctx.model
        ctx.store.save_extraction(source, lm.PROMPT_VERSION, result)
        return source, result, None

    totals = {
        "sources": 0,
        "ideas": 0,
        "dropped_quotes": 0,
        "dropped_ideas": 0,
        "cost_usd": 0.0,
        "failed": [],
    }
    for source, result, error in ctx.parallel(one, pending):
        if error:
            totals["failed"].append({"source": source["id"], "error": error})
            say(f"  {source['title']}: failed ({error})")
            continue
        assert result is not None
        totals["sources"] += 1
        totals["ideas"] += len(result["ideas"])
        totals["dropped_quotes"] += len(result["dropped_quotes"])
        totals["dropped_ideas"] += len(result["dropped_ideas"])
        totals["cost_usd"] += result["cost_usd"]
        say(f"  {source['title']}: {len(result['ideas'])} ideas")
    totals["cost_usd"] = round(totals["cost_usd"], 4)
    say(
        f"Found {totals['ideas']} ideas in {totals['sources']} sources; {totals['dropped_quotes']} quotes that did not match "
        f"their source word for word were dropped. Cost ${totals['cost_usd']:.2f}."
    )
    if totals["failed"]:
        say(f"{len(totals['failed'])} sources failed; run extract again to retry them.")
    return totals


# ---- organize ------------------------------------------------------------------


def load_library(ctx: Context) -> dict[str, Any] | None:
    return read_json(ctx.library_file, None)


def candidates(ctx: Context) -> list[dict[str, Any]]:
    """Ideas found in source versions the library does not hold yet, each with a ``cid``."""
    built = ctx.store.built()
    out = []
    for source in ctx.store.all_sources().values():
        if source.get("status") != "ok" or built.get(source["id"]) == source["sha256"]:
            continue
        extraction = ctx.store.load_extraction(source, lm.PROMPT_VERSION)
        if extraction is None:
            continue
        for n, idea in enumerate(extraction["ideas"]):
            out.append(
                {**idea, "cid": f"{source['id']}#{n + 1}", "source_id": source["id"]}
            )
    return out


def step_organize(ctx: Context) -> dict[str, Any]:
    found = candidates(ctx)
    library = load_library(ctx) or lb.empty_library(None)
    if not found:
        say("No new ideas to sort.")
        result: dict[str, Any] = {
            "chapters": library.get("chapters") or [],
            "placements": [],
            "cost_usd": 0.0,
        }
    else:
        chapters = [dict(c) for c in library.get("chapters") or []]
        for node in library["nodes"]:
            if node["group"] not in [c["name"] for c in chapters]:
                chapters.append({"name": node["group"], "subtitle": ""})
        say(f"Sorting {len(found)} new ideas into chapters ...")
        result = lm.organize(chapters, library["nodes"], found, ctx.complete())
        merges = sum(1 for p in result["placements"] if "merge_into" in p)
        say(
            f"{len(result['chapters'])} chapters; {merges} new ideas merged into ideas that say the same thing."
        )
        for chapter in result["chapters"]:
            say(f"  {chapter['name']}: {chapter.get('subtitle', '')}")
    result["cids"] = sorted(c["cid"] for c in found)
    ctx.store.save_work("organized", result)
    return {
        "candidates": len(found),
        "chapters": len(result["chapters"]),
        "cost_usd": result["cost_usd"],
    }


# ---- connect -------------------------------------------------------------------


def _organized(ctx: Context, found: list[dict[str, Any]]) -> dict[str, Any]:
    organized = ctx.store.work("organized")
    if organized is None or organized.get("cids") != sorted(c["cid"] for c in found):
        raise StepError("the ideas changed since they were sorted; run organize first")
    return organized


def preview(
    ctx: Context,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """The library as it would be with the sorted new ideas added (no connections yet)."""
    found = candidates(ctx)
    organized = _organized(ctx, found)
    library = load_library(ctx) or lb.empty_library(None)
    library = json.loads(json.dumps(library))
    merged = lb.merge(library, candidates=found, organized=organized, connections=[])
    return library, merged, found


def step_connect(ctx: Context) -> dict[str, Any]:
    library, merged, found = preview(ctx)
    new_ids = set(merged["new_ideas"])
    if not new_ids:
        say("No new ideas to connect.")
        ctx.store.save_work(
            "connections", {"cids": sorted(c["cid"] for c in found), "connections": []}
        )
        return {"connections": 0, "cost_usd": 0.0}
    records = ctx.store.all_sources()
    sources = {sid: r for sid, r in records.items() if r.get("status") == "ok"}
    for source in library["sources"]:
        sources.setdefault(
            source["id"],
            {
                "id": source["id"],
                "title": source["title"],
                "raw": source.get("raw", ""),
            },
        )
    new_nodes = [n for n in library["nodes"] if n["id"] in new_ids]
    say(f"Connecting {len(new_nodes)} new ideas ...")
    result = lm.connect(
        new_nodes,
        library["nodes"],
        sources,
        library["edges"],
        ctx.complete(),
        map_parallel=ctx.parallel,
    )
    node_to_cid = {v: k for k, v in merged["cid_to_node"].items() if v in new_ids}
    # Store connections by candidate id, so build can place them whatever ids build assigns.
    connections = [
        {
            **c,
            "source": node_to_cid.get(c["source"], c["source"]),
            "target": node_to_cid.get(c["target"], c["target"]),
        }
        for c in result["connections"]
    ]
    ctx.store.save_work(
        "connections",
        {"cids": sorted(c["cid"] for c in found), "connections": connections},
    )
    expressed = sum(1 for c in connections if c["basis"] == "expressed")
    stats = result["stats"]
    say(
        f"{len(connections)} connections ({expressed} expressed by the author, {len(connections) - expressed} suggested); "
        f"{stats['downgraded']} marked suggested because their quote did not match the source. Cost ${result['cost_usd']:.2f}."
    )
    return {
        "connections": len(connections),
        "expressed": expressed,
        **stats,
        "cost_usd": result["cost_usd"],
    }


# ---- build ---------------------------------------------------------------------


def _asset_paths(
    ctx: Context, record: dict[str, Any], owned: dict[str, str]
) -> tuple[str | None, str | None, str | None]:
    """(saved copy of the original when it has no web link, full transcript, audio) for one source,
    copied into map-assets."""
    origin = Path(record.get("origin_file") or "")
    copy = transcript = audio = None
    # A source with a web link is reached there; one without gets a saved copy to open.
    if origin.is_file() and not record.get("url"):
        copy = lb.copy_asset(
            origin,
            ctx.library_dir,
            f"map-assets/sources/{record['id']}{origin.suffix.lower()}",
            owned,
            record["sha256"],
        )
    if record.get("segments"):
        transcript = lb.write_asset_text(
            f"{record['title']}\n\n{record['text']}\n",
            ctx.library_dir,
            f"map-assets/transcripts/{record['id']}.txt",
            owned,
            record["sha256"],
        )
    if record.get("audio"):
        audio_file = Path(record["origin_file"]).parent / Path(record["audio"]).name
        if not audio_file.is_file():
            audio_file = Path(ctx.settings.get("sources", "")) / record["audio"]
        if audio_file.is_file():
            audio = lb.copy_asset(
                audio_file,
                ctx.library_dir,
                f"map-assets/audio/{record['id']}{audio_file.suffix.lower()}",
                owned,
                ls.file_sha256(audio_file),
            )
    return copy, transcript, audio


def step_build(ctx: Context) -> dict[str, Any]:
    args = ctx.args
    found = candidates(ctx)
    organized = _organized(ctx, found) if found else {"chapters": [], "placements": []}
    connections_work = ctx.store.work("connections") if found else {"connections": []}
    if found and (
        connections_work is None
        or connections_work.get("cids") != sorted(c["cid"] for c in found)
    ):
        raise StepError(
            "the ideas changed since they were connected; run connect first"
        )
    assert connections_work is not None
    if getattr(args, "title", None):
        ctx.settings["title"] = args.title
    if getattr(args, "display_name", None):
        ctx.settings["display_name"] = args.display_name
    existing = load_library(ctx)
    existing_text = json.loads(json.dumps(existing)) if existing is not None else None
    data = (
        existing
        if existing is not None
        else lb.empty_library(ctx.settings.get("title"))
    )
    records = ctx.store.all_sources()
    report: dict[str, Any] = {"library": str(ctx.library_file)}
    built_before = ctx.store.built()
    report["refresh"] = lb.refresh_changed_sources(
        data, {k: v for k, v in records.items() if k in built_before}
    )
    merged = lb.merge(
        data,
        candidates=found,
        organized=organized,
        connections=connections_work["connections"],
    )
    report.update({k: v for k, v in merged.items() if k != "cid_to_node"})
    owned = ctx.store.assets()
    needed: set[str] = set()
    urls: dict[str, str] = {}
    library_sources = {s["id"]: s for s in data["sources"]}
    referenced = lb.referenced_source_ids(data)
    for record in records.values():
        copy, transcript, audio = _asset_paths(ctx, record, owned)
        needed.update(p for p in (copy, transcript, audio) if p)
        urls[record["id"]] = record.get("url") or copy or ""
        if record["id"] in referenced and record.get("status") == "ok":
            entry = library_sources.get(record["id"])
            fresh = lb.library_source(record, copy, transcript, audio)
            if entry is None:
                data["sources"].append(fresh)
                library_sources[record["id"]] = fresh
            else:
                # Keep the library's own title, kind and link (hand edits); refresh the files.
                for key in ("audio", "transcript"):
                    if fresh.get(key):
                        entry[key] = fresh[key]
                entry.setdefault("url", fresh["url"])
    lb.finish(
        data,
        title=ctx.settings.get("title")
        if existing is None or getattr(args, "title", None)
        else None,
        ingested=len(records),
        given={
            "author": getattr(args, "display_name", None),
            "collection": getattr(args, "collection", None),
        },
        defaults={
            "author": ctx.settings.get("display_name")
            or (ctx.settings.get("authors") or [None])[0],
            "collection": organized.get("collection"),
            "reading_intro": organized.get("reading_intro"),
        },
    )
    lb.lay_out(data)
    errors, warnings = lb.validate_library(data, strict_quotes=False)
    new_items = set(merged["new_ideas"])
    strict_errors, _ = lb.validate_library(
        {
            **data,
            "nodes": [n for n in data["nodes"] if n["id"] in new_items],
            "edges": [],
        },
        strict_quotes=True,
    )
    errors += [e for e in strict_errors if "not found in source" in e]
    if errors:
        raise StepError(
            "the library would not pass its check, so nothing was written:\n  "
            + "\n  ".join(errors[:20])
        )
    report["warnings"] = warnings
    report["changed"] = existing_text is None or _without_build_time(
        existing_text
    ) != _without_build_time(data)
    if report["changed"]:
        report["backup"] = str(ctx.store.backup_library(ctx.library_file) or "")
        write_json(ctx.library_file, data)
    formats = read_json(ctx.store.root / "formats.json", {})
    for cid, node_id in merged["cid_to_node"].items():
        candidate = next((c for c in found if c["cid"] == cid), None)
        if candidate and candidate.get("formats") and node_id not in formats:
            formats[node_id] = candidate["formats"]
    write_json(ctx.store.root / "formats.json", formats)
    report["companions"] = []
    sample_path = ctx.library_dir / SAMPLE_FILE
    if lb.may_overwrite(sample_path, f'"generated_by": "{lb.GENERATOR}"'):
        write_json(sample_path, lb.classic_sample(data, formats), indent=1)
        report["companions"].append(SAMPLE_FILE)
    corpus_path = ctx.library_dir / CORPUS_FILE
    if lb.may_overwrite(corpus_path, lb.GENERATED_MARKER):
        write_text(
            corpus_path,
            lb.corpus_page(
                CORPUS_TEMPLATE.read_text(encoding="utf-8"), data, records, urls
            ),
        )
        report["companions"].append(CORPUS_FILE)
    report["pruned_assets"] = lb.prune_assets(ctx.library_dir, owned, needed)
    ctx.store.save_assets(owned)
    built = ctx.store.built()
    for record in records.values():
        if (
            record.get("status") == "ok"
            and ctx.store.load_extraction(record, lm.PROMPT_VERSION) is not None
        ):
            built[record["id"]] = record["sha256"]
    ctx.store.save_built(built)
    ctx.store.save_settings(ctx.settings)
    ctx.store.clear_work()
    refresh = report["refresh"]
    say(
        f"Library {'written' if report['changed'] else 'unchanged'}: {len(data['nodes'])} ideas in {len(data.get('chapters') or [])} chapters, "
        f"{len(data['edges'])} connections, {len(data['sources'])} sources "
        f"(+{len(merged['new_ideas'])} ideas, {merged['merged']} merged, +{merged['new_connections']} connections)."
    )
    if refresh["stale_quotes"]:
        say(
            f"  {len(refresh['stale_quotes'])} quotes no longer in their changed source were dropped."
        )
    if refresh["removed_ideas"]:
        say(
            f"  Removed {len(refresh['removed_ideas'])} ideas left with no quote: "
            + "; ".join(refresh["removed_ideas"])
        )
    if warnings:
        say(
            f"  {len(warnings)} warnings about content that was already there (see the run report)."
        )
    return report


def _without_build_time(data: dict[str, Any]) -> dict[str, Any]:
    """The library minus the build time, to tell whether a build changed anything."""
    copy = json.loads(json.dumps(data))
    copy.get("coverage", {}).pop("last_built", None)
    return copy


# ---- validate -------------------------------------------------------------------


def step_validate(ctx: Context) -> dict[str, Any]:
    path = Path(ctx.args.file) if getattr(ctx.args, "file", None) else ctx.library_file
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StepError(f"could not read {path}: {exc}") from exc
    errors, warnings = lb.validate_library(data, strict_quotes=ctx.args.strict)
    for error in errors:
        say(f"error: {error}")
    for warning in warnings:
        say(f"warning: {warning}")
    say(
        f"{path}: {'ok' if not errors else f'{len(errors)} errors'}; {len(warnings)} warnings."
    )
    if errors:
        raise SystemExit(1)
    return {"errors": errors, "warnings": warnings}


# ---- fetchers and transcription ------------------------------------------------------


def _print_fetch(name: str, report: dict[str, Any]) -> dict[str, Any]:
    parts = [
        f"{len(v)} {k.replace('_', ' ')}"
        for k, v in report.items()
        if isinstance(v, list) and v
    ]
    say(f"{name}: " + (", ".join(parts) or "nothing found"))
    for failure in report.get("failed", []):
        say(
            f"  failed {failure['id'] if 'id' in failure else failure.get('slug')}: {failure['error']}"
        )
    return report


def step_fetch_substack(ctx: Context) -> dict[str, Any]:
    import fetchers

    out = ctx.sources_dir() / "substack"
    return _print_fetch(
        "Substack",
        fetchers.fetch_substack(ctx.args.publication, out, limit=ctx.args.limit),
    )


def step_fetch_plaud(ctx: Context) -> dict[str, Any]:
    import fetchers

    out = ctx.sources_dir() / "plaud"
    a = ctx.args
    report = fetchers.fetch_plaud(
        out,
        since=a.since,
        until=a.until,
        limit=a.limit,
        exclude_title=a.exclude_title,
        audio_without_transcript=a.audio,
    )
    return _print_fetch("Plaud", report)


def step_fetch_otter(ctx: Context) -> dict[str, Any]:
    import fetchers

    out = ctx.sources_dir() / "otter"
    a = ctx.args
    return _print_fetch(
        "Otter.ai",
        fetchers.fetch_otter(
            out,
            query=a.query,
            since=a.since,
            limit=a.limit,
            exclude_title=a.exclude_title,
        ),
    )


def step_transcribe(ctx: Context) -> dict[str, Any]:
    argv = [
        "uv",
        "run",
        str(SCRIPTS / "transcribe.py"),
        "--sources",
        str(ctx.sources_dir()),
        "--model",
        ctx.args.whisper_model,
    ]
    code = subprocess.run(argv).returncode
    if code:
        raise StepError("some recordings could not be transcribed (see above)")
    return {"exit": code}


def step_all(ctx: Context) -> dict[str, Any]:
    results: dict[str, Any] = {"ingest": step_ingest(ctx)}
    results["estimate"] = step_estimate(ctx)
    budget = getattr(ctx.args, "max_cost", None)
    if budget is not None and results["estimate"]["estimated_cost_usd"] > budget:
        raise StepError(
            f"the estimate (${results['estimate']['estimated_cost_usd']:.2f}) is over --max-cost ${budget:.2f}; nothing was spent"
        )
    results["extract"] = step_extract(ctx)
    results["organize"] = step_organize(ctx)
    results["connect"] = step_connect(ctx)
    results["build"] = step_build(ctx)
    return results


# ---- command line ---------------------------------------------------------------------

STEPS: dict[str, Callable[[Context], dict[str, Any]]] = {
    "fetch-substack": step_fetch_substack,
    "fetch-plaud": step_fetch_plaud,
    "fetch-otter": step_fetch_otter,
    "transcribe": step_transcribe,
    "ingest": step_ingest,
    "estimate": step_estimate,
    "extract": step_extract,
    "organize": step_organize,
    "connect": step_connect,
    "build": step_build,
    "validate": step_validate,
    "all": step_all,
}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--sources",
        help=f"the folder of source material (default {DEFAULT_SOURCES_DIR}, remembered)",
    )
    common.add_argument(
        "--library-dir",
        default=str(DEFAULT_LIBRARY_DIR),
        help="the Idea Library app's data folder",
    )
    common.add_argument(
        "--state-dir", default=str(DEFAULT_STATE_DIR), help="this skill's working store"
    )
    common.add_argument(
        "--model",
        help=f"the Claude model for the model steps (default {lm.DEFAULT_MODEL}, remembered)",
    )
    common.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, help="model calls run at once"
    )
    ingest_opts = argparse.ArgumentParser(add_help=False)
    ingest_opts.add_argument(
        "--author",
        action="append",
        help="the author's speaker name in transcripts (repeatable, remembered)",
    )
    ingest_opts.add_argument(
        "--exclude",
        action="append",
        help="a file pattern to keep out, e.g. 'private/*' (repeatable, remembered)",
    )
    build_opts = argparse.ArgumentParser(add_help=False)
    build_opts.add_argument("--title", help="the library's title")
    build_opts.add_argument(
        "--collection",
        help="the collection's name, shown in the map's wordmark (default: named from the material)",
    )
    build_opts.add_argument(
        "--display-name", help="the author's name as the pages should show it"
    )

    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="step", required=True)
    p = sub.add_parser(
        "fetch-substack",
        parents=[common],
        help="save a Substack publication's public posts",
    )
    p.add_argument(
        "publication",
        help="the publication: name, name.substack.com, or its custom domain",
    )
    p.add_argument("--limit", type=int)
    for name, helptext in (
        ("fetch-plaud", "save Plaud transcripts"),
        ("fetch-otter", "save Otter.ai transcripts"),
    ):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--since", help="only recordings from this date on (YYYY-MM-DD)")
        p.add_argument("--limit", type=int)
        p.add_argument(
            "--exclude-title",
            help="a regular expression: recordings whose title matches stay out",
        )
        if name == "fetch-plaud":
            p.add_argument(
                "--until", help="only recordings up to this date (YYYY-MM-DD)"
            )
            p.add_argument(
                "--audio",
                action="store_true",
                help="save the audio of recordings with no transcript, to transcribe",
            )
        else:
            p.add_argument("--query", help="only conversations matching this search")
    p = sub.add_parser(
        "transcribe", parents=[common], help="transcribe audio with no transcript yet"
    )
    p.add_argument("--whisper-model", default="base.en")
    sub.add_parser(
        "ingest", parents=[common, ingest_opts], help="read the sources folder"
    )
    sub.add_parser(
        "estimate", parents=[common], help="projected cost of the model steps"
    )
    sub.add_parser(
        "extract", parents=[common], help="find ideas in new or changed sources"
    )
    sub.add_parser("organize", parents=[common], help="sort new ideas into chapters")
    sub.add_parser("connect", parents=[common], help="connect new ideas")
    sub.add_parser(
        "build", parents=[common, build_opts], help="merge, check and write the library"
    )
    p = sub.add_parser("validate", parents=[common], help="check a library file")
    p.add_argument(
        "file", nargs="?", help="the library file (default: the app's map-data.json)"
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="treat a quote missing from its source as an error",
    )
    p = sub.add_parser(
        "all",
        parents=[common, ingest_opts, build_opts],
        help="ingest, extract, organize, connect, build",
    )
    p.add_argument(
        "--max-cost",
        type=float,
        help="stop before spending when the estimate is over this many dollars",
    )
    return parser


def run(argv: list[str], complete: lm.Complete | None = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    ctx = Context(args, complete)
    if getattr(args, "model", None):
        ctx.settings["model"] = args.model
    began = time.monotonic()
    result = STEPS[args.step](ctx)
    if args.step in ("all", "build", "extract", "organize", "connect", "ingest"):
        ctx.store.save_run_report(
            {
                "step": args.step,
                "argv": argv,
                "seconds": round(time.monotonic() - began, 1),
                "result": result,
            }
        )
    return result


def main() -> int:
    try:
        run(sys.argv[1:])
    except StepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
