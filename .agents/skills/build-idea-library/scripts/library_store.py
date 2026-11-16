"""Where the skill keeps its working state, and how each store is bounded.

``data/.skills/build-idea-library/`` (``--state-dir``):

- ``settings.json``: the sources folder, author names, exclusions, title -- remembered so
  a later ``all`` needs no arguments
- ``sources/<id>.json``: every ingested source, its full text and origin (the raw record)
- ``extractions/<id>-<sha12>-v<prompt>.json``: the checked ideas found in one version of a
  source; a re-run never pays twice for the same text. A source's older versions are
  evicted when it changes.
- ``built.json``: which version of each source the library already holds
- ``work/``: the current run's intermediate files (sorted ideas, connections)
- ``runs/``: the last ``KEEP_RUNS`` run reports
- ``backups/``: the last ``KEEP_BACKUPS`` versions of the library file
- ``assets.json``: the files this skill copied into the app's ``map-assets/``, so it only
  ever prunes its own
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_STATE_DIR = REPO_ROOT / "data/.skills/build-idea-library"
DEFAULT_LIBRARY_DIR = REPO_ROOT / "data/.apps/idea-library"
DEFAULT_SOURCES_DIR = REPO_ROOT / "data/idea-library-sources"
KEEP_RUNS = 20
KEEP_BACKUPS = 5


def now_stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any, *, indent: int | None = 1) -> None:
    """Write ``value`` to ``path`` in one step: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=indent)
            out.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def keep_newest(directory: Path, pattern: str, keep: int) -> list[Path]:
    """Delete all but the ``keep`` newest files matching ``pattern`` (names sort by time)."""
    files = sorted(directory.glob(pattern))
    doomed = files[:-keep] if keep else files
    for path in doomed:
        path.unlink(missing_ok=True)
    return doomed


class Store:
    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir
        self.sources_dir = state_dir / "sources"
        self.extractions_dir = state_dir / "extractions"
        self.work_dir = state_dir / "work"
        self.runs_dir = state_dir / "runs"
        self.backups_dir = state_dir / "backups"

    # settings ---------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return read_json(self.root / "settings.json", {})

    def save_settings(self, settings: dict[str, Any]) -> None:
        write_json(self.root / "settings.json", settings)

    # sources ----------------------------------------------------------------
    def source_ids_by_path(self) -> dict[str, str]:
        return read_json(self.root / "source-ids.json", {})

    def save_source_ids(self, mapping: dict[str, str]) -> None:
        write_json(self.root / "source-ids.json", mapping)

    def load_source(self, source_id: str) -> dict[str, Any] | None:
        return read_json(self.sources_dir / f"{source_id}.json", None)

    def save_source(self, record: dict[str, Any]) -> None:
        write_json(self.sources_dir / f"{record['id']}.json", record)

    def all_sources(self) -> dict[str, dict[str, Any]]:
        if not self.sources_dir.exists():
            return {}
        out = {}
        for path in sorted(self.sources_dir.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            out[record["id"]] = record
        return out

    # extractions --------------------------------------------------------------
    def extraction_path(self, source: dict[str, Any], prompt_version: str) -> Path:
        return (
            self.extractions_dir
            / f"{source['id']}-{source['sha256'][:12]}-v{prompt_version}.json"
        )

    def load_extraction(
        self, source: dict[str, Any], prompt_version: str
    ) -> dict[str, Any] | None:
        return read_json(self.extraction_path(source, prompt_version), None)

    def save_extraction(
        self, source: dict[str, Any], prompt_version: str, result: dict[str, Any]
    ) -> None:
        write_json(self.extraction_path(source, prompt_version), result)

    def evict_other_extractions(
        self, source: dict[str, Any], prompt_version: str
    ) -> list[Path]:
        """Remove cached extractions of ``source`` other than its current version's."""
        keep = self.extraction_path(source, prompt_version)
        doomed = [
            p
            for p in self.extractions_dir.glob(f"{source['id']}-*.json")
            if p != keep and _is_for(p, source["id"])
        ]
        for path in doomed:
            path.unlink(missing_ok=True)
        return doomed

    # built versions -------------------------------------------------------------
    def built(self) -> dict[str, str]:
        return read_json(self.root / "built.json", {})

    def save_built(self, built: dict[str, str]) -> None:
        write_json(self.root / "built.json", built)

    # per-run work ----------------------------------------------------------------
    def work(self, name: str) -> Any:
        return read_json(self.work_dir / f"{name}.json", None)

    def save_work(self, name: str, value: Any) -> None:
        write_json(self.work_dir / f"{name}.json", value)

    def clear_work(self) -> None:
        if self.work_dir.exists():
            for path in self.work_dir.glob("*.json"):
                path.unlink()

    # run reports and backups --------------------------------------------------------
    def save_run_report(self, report: dict[str, Any]) -> Path:
        path = self.runs_dir / f"run-{now_stamp()}.json"
        write_json(path, report)
        keep_newest(self.runs_dir, "run-*.json", KEEP_RUNS)
        return path

    def backup_library(self, library_file: Path) -> Path | None:
        if not library_file.exists():
            return None
        path = self.backups_dir / f"map-data-{now_stamp()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(library_file.read_bytes())
        keep_newest(self.backups_dir, "map-data-*.json", KEEP_BACKUPS)
        return path

    # assets this skill owns ------------------------------------------------------------
    def assets(self) -> dict[str, str]:
        return read_json(self.root / "assets.json", {})

    def save_assets(self, assets: dict[str, str]) -> None:
        write_json(self.root / "assets.json", assets)


def _is_for(path: Path, source_id: str) -> bool:
    """Whether an extraction file name belongs to ``source_id`` and not to an id that
    merely starts with it (``essay`` vs ``essay-2``)."""
    rest = path.stem[len(source_id) + 1 :]
    parts = rest.split("-")
    return len(parts) == 2 and len(parts[0]) == 12 and parts[1].startswith("v")
