"""Idea Library: the user's collected ideas as a reading page, an idea map, and the earlier classic view.

Services run from /home/user/workspace (the repo root). Conventions:

- The pages are code and ship in this package (``assets/``). The data they
  load -- ``map-data.json``, ``sample.json``, the
  ``map-assets/`` audio and transcripts, and the optional ``corpus.html`` --
  is the user's and lives under ``DATA_DIR`` (defined below), never in the
  repo. ``DATA_DIR`` defaults to ``data/.apps/idea-library/`` but honors the
  ``IDEA_LIBRARY_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store (see
  the update-app skill).
- Listen address: bind ``HOST``/``PORT`` (defined below), which default to
  loopback and this app's assigned port but honor ``IDEA_LIBRARY_HOST`` and
  ``IDEA_LIBRARY_PORT``. Never hardcode the port at the ``run_simple`` call.
- The ideas quote automatic transcripts: never log their content. Requests are
  logged by path only, and no route logs a body.

The pages load their data by relative URL (``map-data.json``,
``map-assets/<file>``), so every page and data file is served at the root of
the app's own origin, exactly where the pages expect them.
"""

import base64
import functools
import hashlib
import os
import re
from pathlib import Path

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import model_response
from flask import Flask, Response, abort, request, send_file, send_from_directory
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple

# The user's data: read through DATA_DIR only, so the env override is never bypassed.
DATA_DIR = Path(os.environ.get("IDEA_LIBRARY_DATA_DIR", "data/.apps/idea-library"))

PORT = int(os.environ.get("IDEA_LIBRARY_PORT", "8093"))
HOST = os.environ.get("IDEA_LIBRARY_HOST", "127.0.0.1")

# The pages, shipped with the code.
ASSETS_DIR = Path(__file__).parent / "assets"
READING_PAGE = "textbook.html"
MAP_PAGE = "map.html"
CLASSIC_PAGE = "classic.html"
GRAPH_PAGE = "graph.html"

# The data files the pages fetch, served from DATA_DIR at the same names.
DATA_FILE_NAMES = ("map-data.json", "sample.json")
# The audio clips and full transcripts the idea map links to (``map-assets/<file>``).
MAP_ASSETS_DIR_NAME = "map-assets"
# A generated, self-contained page of the whole transcript corpus the idea map links to.
CORPUS_PAGE = "corpus.html"

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path(
    "system/apps/system_interface/imbue/system_interface/static/_static"
)
SHELL_STATIC_MODULE_NAMES = ("app_contract.js", "context_menu.js")

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT_BODY = """
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
"""
SHELL_PAGE_SCRIPT = f'<script type="module">{SHELL_PAGE_SCRIPT_BODY}</script>'

# An inline <script> (one with no src): what the policy has to admit by hash.
_INLINE_SCRIPT_PATTERN = re.compile(
    r"<script\b(?P<attributes>[^>]*)>(?P<body>.*?)</script\s*>",
    re.DOTALL | re.IGNORECASE,
)
_SRC_ATTRIBUTE_PATTERN = re.compile(r"\bsrc\s*=", re.IGNORECASE)


def inline_script_source(body: str) -> str:
    """The Content-Security-Policy source admitting one inline <script> whose body is exactly ``body``."""
    return (
        "'sha256-"
        + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        + "'"
    )


def inline_script_bodies(html: str) -> list[str]:
    """The body of every inline <script> in ``html``, in page order."""
    return [
        match.group("body")
        for match in _INLINE_SCRIPT_PATTERN.finditer(html)
        if not _SRC_ATTRIBUTE_PATTERN.search(match.group("attributes"))
    ]


def content_security_policy(extra_inline_scripts: list[str]) -> str:
    """The policy every page is served under, admitting the shell page script and ``extra_inline_scripts`` by hash.

    Scripts only from this origin plus the inline blocks admitted by hash; fetches,
    media and images only from this origin; inline styles plus the Google Fonts
    stylesheet and font files the reading page uses; frames of any http(s) page;
    no plugins. The browser posts every block to ``/_csp-report``, which logs it.
    """
    hashes = " ".join(
        dict.fromkeys(
            inline_script_source(body)
            for body in [SHELL_PAGE_SCRIPT_BODY, *extra_inline_scripts]
        )
    )
    return (
        "default-src 'self'; "
        f"script-src 'self' 'report-sample' {hashes}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; media-src 'self'; frame-src http: https:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "report-uri /_csp-report"
    )


# The policy of any HTML answer that is not one of the pages (an error page, say).
CONTENT_SECURITY_POLICY = content_security_policy([])
BROWSER_EXTENSION_SCHEMES = (
    "chrome-extension:",
    "moz-extension:",
    "safari-web-extension:",
)


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the reading page"),
    ("GET", "/index.html"): SchemalessRoute(reason="the reading page"),
    ("GET", "/textbook.html"): SchemalessRoute(reason="the reading page"),
    ("GET", "/map.html"): SchemalessRoute(reason="the idea map page"),
    ("GET", "/classic.html"): SchemalessRoute(reason="the classic view page"),
    ("GET", "/graph.html"): SchemalessRoute(reason="the graph page"),
    ("GET", "/corpus.html"): SchemalessRoute(
        reason="the user's generated transcript-corpus page"
    ),
    ("GET", "/map-data.json"): SchemalessRoute(
        reason="the user's idea data file, served as stored"
    ),
    ("GET", "/sample.json"): SchemalessRoute(
        reason="the user's classic-view data file, served as stored"
    ),
    ("GET", "/map-assets/{subpath}"): SchemalessRoute(
        reason="a voice-note recording or transcript file"
    ),
    ("GET", "/_static/{basename}"): SchemalessRoute(
        reason="a browser-side module the shell builds"
    ),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("POST", "/_csp-report"): SchemalessRoute(
        reason="the browser's report of what the page's policy blocked"
    ),
}

app = Flask("idea_library", static_folder=None)
# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app,
    DEFAULT_API_SCHEMA_PATH,
    title="Idea Library",
    description="Browse the idea library: ideas pulled from voice notes and writing, shown as a searchable list and a map.",
    route_table=ROUTE_TABLE,
)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


@functools.lru_cache(maxsize=16)
def _rendered_page(path: Path, modified_ns: int, size: int) -> tuple[str, str]:
    """The page at ``path`` with the shell page script added, and the policy admitting its inline scripts.

    Keyed by the file's modification time and size, so an edited page is re-read on its next request.
    """
    html = path.read_text(encoding="utf-8")
    closing = html.rfind("</body>")
    rendered = (
        html[:closing] + SHELL_PAGE_SCRIPT + html[closing:]
        if closing >= 0
        else html + SHELL_PAGE_SCRIPT
    )
    return rendered, content_security_policy(inline_script_bodies(html))


def _page_response(name: str) -> Response:
    path = ASSETS_DIR / name
    stat = path.stat()
    html, policy = _rendered_page(path, stat.st_mtime_ns, stat.st_size)
    response = Response(html, mimetype="text/html")
    response.headers["Content-Security-Policy"] = policy
    # Pages ship with the code: revalidate so a new version is never hidden behind a cached copy.
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/")
@app.route("/index.html")
@app.route("/textbook.html")
def reading_page() -> Response:
    """The reading page: the ideas as chapters, threads, and sources, with search and deep links to each idea."""
    return _page_response(READING_PAGE)


@app.route("/map.html")
def map_page() -> Response:
    """The idea map: every idea and connection drawn as a graph; ``#<idea-id>`` selects that idea."""
    return _page_response(MAP_PAGE)


@app.route("/graph.html")
def graph_page() -> Response:
    """The graph: every idea as a glowing point in a moving layout or on a spinning globe; ``#<idea-id>`` opens that idea."""
    return _page_response(GRAPH_PAGE)


@app.route("/classic.html")
def classic_page() -> Response:
    """The classic view: the earlier card layout of the first sample of ideas."""
    return _page_response(CLASSIC_PAGE)


def _data_file_response(name: str) -> Response:
    # send_from_directory refuses any name that would leave DATA_DIR.
    response = send_from_directory(DATA_DIR.absolute(), name)
    # The user's data changes whenever the library is rebuilt: always revalidate.
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/map-data.json")
@app.route("/sample.json")
def data_file() -> Response:
    """One of the user's data files, exactly as stored; 404 when the library does not have it."""
    name = request.path.lstrip("/")
    if name not in DATA_FILE_NAMES:
        abort(404)
    return _data_file_response(name)


@app.route("/map-assets/<path:subpath>")
def map_asset(subpath: str) -> Response:
    """A voice-note recording or full transcript the idea map links to; 404 when it is missing."""
    return _data_file_response(f"{MAP_ASSETS_DIR_NAME}/{subpath}")


@functools.lru_cache(maxsize=4)
def _corpus_policy(path: Path, modified_ns: int, size: int) -> str:
    """The policy admitting the corpus page's own inline scripts, recomputed only when the file changes."""
    return content_security_policy(
        inline_script_bodies(path.read_text(encoding="utf-8", errors="replace"))
    )


@app.route("/corpus.html")
def corpus_page() -> Response:
    """The generated transcript-corpus page the idea map links to, served as stored; 404 when the library has none."""
    path = DATA_DIR / CORPUS_PAGE
    if not path.is_file():
        abort(404)
    stat = path.stat()
    response = send_file(path.absolute(), mimetype="text/html")
    response.headers["Content-Security-Policy"] = _corpus_policy(
        path.absolute(), stat.st_mtime_ns, stat.st_size
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/_csp-report", methods=["POST"])
def content_security_policy_report() -> Response:
    """Where the browser reports what the page's security policy blocked."""
    body = request.get_json(force=True, silent=True)
    report = body.get("csp-report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return Response(status=204)
    blocked, page, source, directive, line, sample = (
        str(report.get(key, ""))[:500]
        for key in (
            "blocked-uri",
            "document-uri",
            "source-file",
            "effective-directive",
            "line-number",
            "script-sample",
        )
    )
    # A block caused by a browser extension is the policy doing its job and is not logged.
    if blocked.startswith(BROWSER_EXTENSION_SCHEMES) or source.startswith(
        BROWSER_EXTENSION_SCHEMES
    ):
        return Response(status=204)
    # An inline script or style is reported as "inline": say where it is and how it starts instead.
    where = f" at {source}:{line}" if source and line else ""
    starting = f" starting {sample!r}" if sample else ""
    remedy = (
        "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        if blocked == "inline"
        else "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
    )
    logger.error(
        "Content-Security-Policy blocked {!r} on {!r} ({}){}{}; {}",
        blocked,
        page,
        directive,
        where,
        starting,
        remedy,
    )
    return Response(status=204)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    """One of the two browser-side modules the workspace shell builds and every app serves from its own origin."""
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


def main() -> None:
    setup_logging()
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
