"""Fetch source material into the sources folder: Substack posts, Plaud and Otter.ai transcripts.

Every fetcher writes one JSON file per item, named for the platform and the item's id, and
holding the platform's own record (the raw payload) beside the fields the reader needs. A
file already there is skipped, so a re-run fetches only what is new. One item that fails
is reported and the rest carry on.

Plaud and Otter.ai are reached through their official connectors (hosted MCP servers that
sign in with the user's own account), driven with the workspace's ``mcpc`` client. No
password or token passes through this code. See ``references/recording-platforms.md``.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from library_sources import normalize_segments, parse_transcript_lines
from library_store import write_json

SKILL_DIR = Path(__file__).resolve().parents[1]
CONNECTOR_CONFIG = SKILL_DIR / "assets" / "mcp-servers.json"
USER_AGENT = "Mozilla/5.0 (idea-library builder)"
WORKERS = 4


class FetchError(RuntimeError):
    """A fetch that cannot go on (no connection, a refused sign-in)."""


# ---- Substack ----------------------------------------------------------------

HttpGet = Callable[[str], Any]


def http_get_json(url: str, *, attempts: int = 4) -> Any:
    """GET ``url`` as JSON, backing off when the server asks us to slow down."""
    for attempt in range(attempts):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
    raise FetchError(f"gave up on {url}")


def substack_base(publication: str) -> str:
    """``https://name.substack.com`` for ``name``, a substack URL, or a custom domain."""
    publication = publication.strip().rstrip("/")
    if "://" not in publication:
        publication = (
            f"https://{publication}"
            if "." in publication
            else f"https://{publication}.substack.com"
        )
    parts = urllib.parse.urlsplit(publication)
    return f"{parts.scheme}://{parts.netloc}"


def fetch_substack(
    publication: str,
    out_dir: Path,
    *,
    http_get: HttpGet = http_get_json,
    limit: int | None = None,
) -> dict[str, Any]:
    """Save every post of a Substack publication through its public archive endpoint.

    A paid post comes back cut short for an outsider; those are saved and listed as
    ``partial`` (a signed-in export is the way to get them whole)."""
    base = substack_base(publication)
    out_dir.mkdir(parents=True, exist_ok=True)
    slugs: list[str] = []
    offset = 0
    while True:
        page = http_get(f"{base}/api/v1/archive?sort=new&offset={offset}&limit=50")
        if not page:
            break
        for post in page:
            if post.get("slug") and post.get("type", "newsletter") in (
                "newsletter",
                "thread",
                "podcast",
            ):
                slugs.append(post["slug"])
        offset += len(page)
        if limit and len(slugs) >= limit:
            break
    slugs = slugs[:limit] if limit else slugs
    report: dict[str, Any] = {
        "saved": [],
        "already_saved": [],
        "partial": [],
        "failed": [],
    }

    def one(slug: str) -> tuple[str, str, str | None]:
        path = out_dir / f"substack-{slug}.json"
        if path.exists():
            return slug, "already_saved", None
        try:
            post = http_get(f"{base}/api/v1/posts/{urllib.parse.quote(slug)}")
        except Exception as exc:  # one post failing must not sink the rest
            return slug, "failed", f"{type(exc).__name__}: {exc}"[:200]
        record = {
            "platform": "substack",
            "title": post.get("title"),
            "subtitle": post.get("subtitle"),
            "url": post.get("canonical_url") or f"{base}/p/{slug}",
            "date": post.get("post_date"),
            "audience": post.get("audience"),
            "body_html": post.get("body_html") or "",
            "post": post,
        }
        write_json(path, record)
        partial = post.get("audience") not in (None, "everyone") or bool(
            post.get("truncated_body_text")
        )
        return slug, "partial" if partial else "saved", None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for slug, outcome, error in pool.map(one, slugs):
            if outcome == "failed":
                report["failed"].append({"slug": slug, "error": error})
            else:
                report[outcome].append(slug)
                if outcome == "partial":
                    report["saved"].append(slug)
    return report


# ---- connectors (MCP through mcpc) ---------------------------------------------

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=180)


def unwrap_tool_text(text: str) -> Any:
    """The JSON a connector tool returned, out of any wrapper text around it (Plaud wraps
    results in an ``<untrusted-user-data ...>`` block with a sentence before it)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return text
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return text


class Connector:
    """One official connector session (``@plaud``, ``@otter``) driven through mcpc."""

    def __init__(
        self, name: str, *, runner: Runner = _run, config: Path = CONNECTOR_CONFIG
    ) -> None:
        self.name = name
        self.session = f"@{name}"
        self.runner = runner
        self.config = config
        servers = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
        self.url = servers[name]["url"]

    def ensure(self) -> None:
        """Open the session if needed; fail with what the user must do when not signed in."""
        listing = self.runner(["mcpc", "--json", self.session, "tools-list"])
        if listing.returncode == 0:
            return
        connected = self.runner(
            ["mcpc", "connect", f"{self.config}:{self.name}", self.session]
        )
        listing = self.runner(["mcpc", "--json", self.session, "tools-list"])
        if listing.returncode != 0:
            detail = (
                connected.stderr or connected.stdout or listing.stderr or ""
            ).strip()[:300]
            raise FetchError(
                f"not connected to {self.name}: the user must sign in once ({self.url}). "
                f"Follow the connect-external-service skill's MCP sign-in, then run this again. ({detail})"
            )

    def tool_schema(self, tool: str) -> dict[str, Any]:
        result = self.runner(["mcpc", "--json", self.session, "tools-get", tool])
        if result.returncode != 0:
            raise FetchError(
                f"{self.name} has no tool '{tool}': {result.stderr.strip()[:200]}"
            )
        return json.loads(result.stdout)

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        result = self.runner(
            ["mcpc", "--json", self.session, "tools-call", tool, json.dumps(arguments)]
        )
        try:
            payload = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        if result.returncode != 0 or payload.get("isError"):
            message = " ".join(
                c.get("text", "")
                for c in payload.get("content", [])
                if isinstance(c, dict)
            )
            raise FetchError(
                f"{self.name} {tool} failed: {(message or result.stderr).strip()[:300]}"
            )
        if "structuredContent" in payload and payload["structuredContent"] is not None:
            return payload["structuredContent"]
        texts = [
            c.get("text", "")
            for c in payload.get("content", [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        return unwrap_tool_text("\n".join(texts))


# ---- Plaud ----------------------------------------------------------------------


def plaud_hex(file_id: str) -> str:
    return file_id[3:] if file_id.startswith("of_") else file_id


def list_plaud_files(
    connector: Connector, *, since: str | None, until: str | None, limit: int | None
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    page = 1
    while True:
        arguments: dict[str, Any] = {"page": page, "page_size": 100}
        if since:
            arguments["date_from"] = since
        if until:
            arguments["date_to"] = until
        result = connector.call("list_files", arguments)
        batch = result.get("data", []) if isinstance(result, dict) else []
        files.extend(batch)
        if len(batch) < 100 or (limit and len(files) >= limit):
            break
        page += 1
    return files[:limit] if limit else files


def plaud_transcript(connector: Connector, file_id: str) -> list[dict[str, Any]]:
    """Every utterance of a recording's transcript, following ``next_cursor``."""
    segments: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(1000):
        arguments: dict[str, Any] = {"file_id": file_id, "limit": 500}
        if cursor:
            arguments["cursor"] = cursor
        page = connector.call("get_transcript", arguments)
        if not isinstance(page, dict):
            break
        batch = page.get("segments") or []
        segments.extend(batch)
        cursor = page.get("next_cursor")
        total = page.get("total")
        if (
            not batch
            or not cursor
            or (isinstance(total, int) and len(segments) >= total)
        ):
            break
    return segments


def fetch_plaud(
    out_dir: Path,
    *,
    connector: Connector | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    exclude_title: str | None = None,
    audio_without_transcript: bool = False,
    download: Callable[[str, Path], None] | None = None,
) -> dict[str, Any]:
    """Save the user's Plaud transcripts, one ``plaud-<id>.json`` per recording.

    ``exclude_title`` (a regular expression) keeps private recordings out. A recording
    with no transcript is listed, or with ``audio_without_transcript`` its audio is saved
    for the transcribe step."""
    connector = connector or Connector("plaud")
    connector.ensure()
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(exclude_title, re.I) if exclude_title else None
    report: dict[str, Any] = {
        "saved": [],
        "already_saved": [],
        "excluded": [],
        "no_transcript": [],
        "audio_saved": [],
        "failed": [],
    }
    todo = []
    for item in list_plaud_files(connector, since=since, until=until, limit=limit):
        file_id = str(item.get("id", ""))
        name = str(item.get("name") or "")
        if not file_id:
            continue
        if pattern and pattern.search(name):
            report["excluded"].append(file_id)
            continue
        path = out_dir / f"plaud-{plaud_hex(file_id)}.json"
        if path.exists() or path.with_suffix(".mp3").exists():
            report["already_saved"].append(file_id)
            continue
        todo.append((item, path))

    def one(job: tuple[dict[str, Any], Path]) -> tuple[str, str, str | None]:
        item, path = job
        file_id = str(item["id"])
        try:
            segments = plaud_transcript(connector, file_id)
            if not normalize_segments(segments):
                if not audio_without_transcript:
                    return file_id, "no_transcript", None
                detail = connector.call("get_file", {"file_id": file_id})
                link = detail.get("presigned_url") if isinstance(detail, dict) else None
                if not link:
                    return file_id, "no_transcript", None
                (download or _download)(link, path.with_suffix(".mp3"))
                return file_id, "audio_saved", None
            detail = connector.call("get_file", {"file_id": file_id})
            # The signed audio link expires in a day and grants access: it is not kept.
            metadata = {
                k: v
                for k, v in (detail if isinstance(detail, dict) else item).items()
                if k != "presigned_url"
            }
            metadata.pop("source_list", None)
            metadata.pop("note_list", None)
            record = {
                "platform": "plaud",
                "id": file_id,
                "title": item.get("name") or file_id,
                "url": f"https://web.plaud.ai/file/{plaud_hex(file_id)}",
                "date": item.get("start_at") or item.get("created_at"),
                "duration_ms": item.get("duration"),
                "segments": segments,
                "file": metadata,
            }
            write_json(path, record)
            return file_id, "saved", None
        except Exception as exc:  # one recording failing must not sink the rest
            return file_id, "failed", f"{type(exc).__name__}: {exc}"[:300]

    with ThreadPoolExecutor(max_workers=3) as pool:
        for file_id, outcome, error in pool.map(one, todo):
            report[outcome].append(
                {"id": file_id, "error": error} if error else file_id
            )
    return report


def _download(url: str, path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    temporary = path.with_name(path.name + ".part")
    with (
        urllib.request.urlopen(request, timeout=120) as response,
        temporary.open("wb") as out,
    ):
        while block := response.read(1 << 16):
            out.write(block)
    temporary.replace(path)


# ---- Otter.ai -----------------------------------------------------------------------

_LIST_KEYS = (
    "results",
    "meetings",
    "conversations",
    "speeches",
    "items",
    "data",
    "matches",
)
_ID_KEYS = ("id", "otid", "speech_id", "conversation_id", "meeting_id")


def _items(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        for key in _LIST_KEYS:
            if isinstance(result.get(key), list):
                return [r for r in result[key] if isinstance(r, dict)]
    return []


def otter_id(item: dict[str, Any]) -> str | None:
    for key in _ID_KEYS:
        if item.get(key):
            return str(item[key])
    url = str(item.get("url") or "")
    match = re.search(r"otter\.ai/u/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None


def _string_properties(schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    input_schema = schema.get("inputSchema") or schema.get("input_schema") or {}
    return input_schema.get("properties") or {}, input_schema.get("required") or []


def otter_search_arguments(
    schema: dict[str, Any], query: str | None, since: str | None
) -> dict[str, Any]:
    """Arguments for Otter's search tool, filled from whatever its schema asks for."""
    properties, required = _string_properties(schema)
    arguments: dict[str, Any] = {}
    for name in properties:
        lowered = name.lower()
        if lowered in ("query", "q", "search", "keywords"):
            arguments[name] = query or ""
        elif since and any(
            word in lowered for word in ("from", "after", "start_date", "since")
        ):
            arguments[name] = since
    for name in required:
        arguments.setdefault(name, query or "")
    return arguments


def otter_fetch_arguments(schema: dict[str, Any], item_id: str) -> dict[str, Any]:
    properties, required = _string_properties(schema)
    for name in [*required, *properties]:
        if name.lower() in (
            "id",
            "otid",
            "speech_id",
            "conversation_id",
            "meeting_id",
            "url",
        ):
            return {
                name: f"https://otter.ai/u/{item_id}"
                if name.lower() == "url"
                else item_id
            }
    return {(required or ["id"])[0]: item_id}


def otter_record(item_id: str, listed: dict[str, Any], fetched: Any) -> dict[str, Any]:
    """The saved record for one Otter conversation, tolerant of how the transcript comes back:
    a list of speaker turns, or text in Otter's ``Name  0:05`` export layout."""
    body: dict[str, Any] = (
        fetched
        if isinstance(fetched, dict)
        else {"text": fetched if isinstance(fetched, str) else ""}
    )
    segments: list[dict[str, Any]] = []
    for key in ("transcript", "segments", "transcripts", "utterances"):
        value = body.get(key)
        if isinstance(value, list):
            segments = normalize_segments(value)
            break
    text = ""
    if not segments:
        for key in ("text", "transcript", "content"):
            if isinstance(body.get(key), str) and body[key].strip():
                text = body[key]
                break
        parsed = parse_transcript_lines(text) if text else None
        segments = parsed or []
    metadata: dict[str, Any] = (
        body["metadata"] if isinstance(body.get("metadata"), dict) else {}
    )
    record: dict[str, Any] = {
        "platform": "otter",
        "id": item_id,
        "title": body.get("title")
        or listed.get("title")
        or metadata.get("title")
        or item_id,
        "url": body.get("url") or listed.get("url") or f"https://otter.ai/u/{item_id}",
        "date": body.get("date")
        or listed.get("date")
        or metadata.get("start_time")
        or listed.get("start_time"),
        "raw": fetched,
    }
    if segments:
        record["segments"] = segments
    else:
        record["text"] = text
    return record


def fetch_otter(
    out_dir: Path,
    *,
    connector: Connector | None = None,
    query: str | None = None,
    since: str | None = None,
    limit: int | None = None,
    exclude_title: str | None = None,
) -> dict[str, Any]:
    """Save the user's Otter.ai conversations, one ``otter-<id>.json`` each, through
    Otter's official connector (its ``search`` and ``fetch`` tools)."""
    connector = connector or Connector("otter")
    connector.ensure()
    out_dir.mkdir(parents=True, exist_ok=True)
    search_schema = connector.tool_schema("search")
    fetch_schema = connector.tool_schema("fetch")
    found = _items(
        connector.call("search", otter_search_arguments(search_schema, query, since))
    )
    pattern = re.compile(exclude_title, re.I) if exclude_title else None
    report: dict[str, Any] = {
        "saved": [],
        "already_saved": [],
        "excluded": [],
        "empty": [],
        "failed": [],
    }
    todo = []
    for item in found[:limit] if limit else found:
        item_id = otter_id(item)
        if not item_id:
            continue
        if pattern and pattern.search(str(item.get("title") or "")):
            report["excluded"].append(item_id)
            continue
        path = out_dir / f"otter-{re.sub(r'[^A-Za-z0-9_-]', '', item_id)}.json"
        if path.exists():
            report["already_saved"].append(item_id)
            continue
        todo.append((item_id, item, path))

    def one(job: tuple[str, dict[str, Any], Path]) -> tuple[str, str, str | None]:
        item_id, item, path = job
        try:
            record = otter_record(
                item_id,
                item,
                connector.call("fetch", otter_fetch_arguments(fetch_schema, item_id)),
            )
        except Exception as exc:  # one conversation failing must not sink the rest
            return item_id, "failed", f"{type(exc).__name__}: {exc}"[:300]
        if not record.get("segments") and not record.get("text", "").strip():
            return item_id, "empty", None
        write_json(path, record)
        return item_id, "saved", None

    with ThreadPoolExecutor(max_workers=3) as pool:
        for item_id, outcome, error in pool.map(one, todo):
            report[outcome].append(
                {"id": item_id, "error": error} if error else item_id
            )
    return report
