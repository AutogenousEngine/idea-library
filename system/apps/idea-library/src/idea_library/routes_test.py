"""The Idea Library's routes, through the Flask test client over the synthetic library."""

import json
import os
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from loguru import logger

from idea_library import runner
from idea_library.runner import (
    SHELL_PAGE_SCRIPT,
    app,
    inline_script_bodies,
    inline_script_source,
)

PAGES = {
    "/": "Idea Library",
    "/index.html": "Idea Library",
    "/textbook.html": "Idea Library",
    "/map.html": "Idea Map",
    "/classic.html": "Idea Library",
    "/graph.html": "Idea Library · Graph",
}


@pytest.fixture
def client(library_dir: Path) -> FlaskClient:
    return app.test_client()


def _directive(policy: str, name: str) -> list[str]:
    """The sources one directive of ``policy`` lists."""
    directive = next(part for part in policy.split(";") if part.split()[0] == name)
    return directive.split()[1:]


def _script_sources(policy: str) -> list[str]:
    return _directive(policy, "script-src")


@pytest.mark.parametrize("path", sorted(PAGES))
def test_each_page_carries_the_shell_page_script_once(
    client: FlaskClient, path: str
) -> None:
    response = client.get(path)
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert f"<title>{PAGES[path]}</title>" in html
    assert html.count(SHELL_PAGE_SCRIPT) == 1
    # The script closes the body, after everything else the page holds.
    assert html.index(SHELL_PAGE_SCRIPT) + len(SHELL_PAGE_SCRIPT) == html.rindex(
        "</body>"
    )


@pytest.mark.parametrize("path", sorted(PAGES))
def test_each_page_policy_admits_exactly_its_inline_scripts(
    client: FlaskClient, path: str
) -> None:
    response = client.get(path)
    html = response.get_data(as_text=True)
    sources = _script_sources(response.headers["Content-Security-Policy"])
    expected = {inline_script_source(body) for body in inline_script_bodies(html)}
    assert expected <= set(sources)
    # Nothing beyond the page's own blocks: no hash for a script the page does not hold, and never 'unsafe-inline'.
    assert {source for source in sources if source.startswith("'sha256-")} == expected
    assert "'unsafe-inline'" not in sources
    assert "*" not in sources


def test_the_policy_admits_the_fonts_the_reading_page_loads(
    client: FlaskClient,
) -> None:
    response = client.get("/")
    policy = response.headers["Content-Security-Policy"]
    assert "https://fonts.googleapis.com" in _directive(policy, "style-src")
    assert "https://fonts.gstatic.com" in _directive(policy, "font-src")
    assert "fonts.googleapis.com/css2" in response.get_data(as_text=True)


def test_the_root_serves_the_reading_page_with_its_three_tabs(
    client: FlaskClient,
) -> None:
    html = client.get("/").get_data(as_text=True)
    assert html == client.get("/textbook.html").get_data(as_text=True)
    for view in ("chapters", "threads", "sources"):
        assert f'data-v="{view}"' in html
    assert 'href="map.html"' in html
    assert 'href="classic.html"' in html


def test_an_edited_page_is_served_fresh(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    page = assets / runner.CLASSIC_PAGE
    page.write_text(
        "<html><body><p>first</p><script>one()</script></body></html>", encoding="utf-8"
    )
    monkeypatch.setattr(runner, "ASSETS_DIR", assets)
    first = client.get("/classic.html")
    assert "first" in first.get_data(as_text=True)
    page.write_text(
        "<html><body><p>second edit</p><script>two()</script></body></html>",
        encoding="utf-8",
    )
    stat = page.stat()
    os.utime(page, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    second = client.get("/classic.html")
    assert "second edit" in second.get_data(as_text=True)
    assert inline_script_source("two()") in second.headers["Content-Security-Policy"]
    assert (
        inline_script_source("one()") not in second.headers["Content-Security-Policy"]
    )


@pytest.mark.parametrize("name", runner.DATA_FILE_NAMES)
def test_each_data_file_is_served_exactly_as_stored(
    client: FlaskClient, library_dir: Path, name: str
) -> None:
    response = client.get(f"/{name}")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.get_data() == (library_dir / name).read_bytes()
    assert response.headers["Cache-Control"] == "no-cache"


def test_quotes_keep_the_link_to_their_source(client: FlaskClient) -> None:
    data = json.loads(client.get("/map-data.json").get_data())
    urls = {source["id"]: source["url"] for source in data["sources"]}
    quoted = [
        evidence["source_id"] for node in data["nodes"] for evidence in node["evidence"]
    ]
    assert quoted
    assert all(urls[source_id].startswith("https://") for source_id in quoted)


@pytest.mark.parametrize("name", runner.DATA_FILE_NAMES)
def test_a_missing_data_file_is_a_404(
    client: FlaskClient, library_dir: Path, name: str
) -> None:
    (library_dir / name).unlink()
    assert client.get(f"/{name}").status_code == 404


def test_an_empty_data_folder_still_serves_every_page(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path / "does-not-exist")
    for path in PAGES:
        assert client.get(path).status_code == 200
    assert client.get("/map-data.json").status_code == 404


def test_map_assets_serve_the_recordings_and_transcripts(
    client: FlaskClient, library_dir: Path
) -> None:
    audio = client.get("/map-assets/lantern.mp3")
    assert audio.status_code == 200
    assert audio.mimetype == "audio/mpeg"
    assert audio.get_data() == (library_dir / "map-assets" / "lantern.mp3").read_bytes()
    transcript = client.get("/map-assets/transcripts/orchard.txt")
    assert transcript.status_code == 200
    assert transcript.mimetype == "text/plain"
    assert transcript.get_data(as_text=True) == "Full transcript of the orchard walk.\n"
    assert client.get("/map-assets/transcripts/missing.txt").status_code == 404


def test_map_assets_never_reach_outside_the_data_folder(
    client: FlaskClient, library_dir: Path
) -> None:
    (library_dir.parent / "secret.txt").write_text(
        "not the library's", encoding="utf-8"
    )
    for path in (
        "/map-assets/../../secret.txt",
        "/map-assets/%2e%2e/%2e%2e/secret.txt",
        "/map-assets//etc/passwd",
    ):
        response = client.get(path, follow_redirects=True)
        assert response.status_code == 404, path
        assert b"not the library's" not in response.get_data()


def test_the_corpus_page_is_a_404_until_the_library_has_one(
    client: FlaskClient,
) -> None:
    assert client.get("/corpus.html").status_code == 404


def test_the_corpus_page_is_served_as_stored_with_its_own_scripts_admitted(
    client: FlaskClient, library_dir: Path
) -> None:
    corpus = "<!doctype html><html><body><h1>Corpus</h1><script>const rows = [1, 2];</script></body></html>"
    (library_dir / "corpus.html").write_text(corpus, encoding="utf-8")
    response = client.get("/corpus.html")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == corpus
    assert inline_script_source("const rows = [1, 2];") in _script_sources(
        response.headers["Content-Security-Policy"]
    )


def test_inline_script_bodies_skip_scripts_loaded_by_src() -> None:
    html = '<script src="/a.js"></script><SCRIPT type="module">x()</SCRIPT><script>\ny()\n</script >'
    assert inline_script_bodies(html) == ["x()", "\ny()\n"]


def test_the_shell_modules_are_served_and_nothing_else(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modules = tmp_path / "_static"
    modules.mkdir()
    (modules / "app_contract.js").write_text(
        "export const connectToShell = () => {};", encoding="utf-8"
    )
    (modules / "other.js").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(runner, "SHELL_STATIC_MODULES_DIR", modules)
    response = client.get("/_static/app_contract.js")
    assert response.status_code == 200
    assert response.mimetype == "text/javascript"
    assert client.get("/_static/other.js").status_code == 404
    # A module the shell has not built yet is a 404, not a crash.
    assert client.get("/_static/context_menu.js").status_code == 404


def test_health(client: FlaskClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_a_blocked_resource_is_logged(client: FlaskClient) -> None:
    logged: list[str] = []
    sink = logger.add(lambda message: logged.append(str(message)), level="ERROR")
    report = {
        "csp-report": {
            "blocked-uri": "https://cdn.example/x.js",
            "document-uri": "http://app/",
            "effective-directive": "script-src",
        }
    }
    response = client.post(
        "/_csp-report", data=json.dumps(report), content_type="application/csp-report"
    )
    assert response.status_code == 204
    extension = {
        "csp-report": {
            "blocked-uri": "chrome-extension://abc",
            "document-uri": "http://app/",
        }
    }
    assert (
        client.post(
            "/_csp-report",
            data=json.dumps(extension),
            content_type="application/csp-report",
        ).status_code
        == 204
    )
    assert client.post("/_csp-report", data="not json").status_code == 204
    logger.remove(sink)
    # The page's block is logged once; the extension's block and the malformed report are not.
    assert len(logged) == 1
    assert (
        "Content-Security-Policy blocked 'https://cdn.example/x.js' on 'http://app/' (script-src)"
        in logged[0]
    )
