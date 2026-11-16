"""Tests for the Substack, Plaud and Otter.ai fetchers against recorded response shapes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import fetchers  # noqa: E402
import library_sources as ls  # noqa: E402

FIXTURES = SCRIPTS.parent / "tests" / "fixtures"


class FakeMcpc:
    """Stands in for the ``mcpc`` command: answers tool calls from a table."""

    def __init__(
        self,
        tools: dict[str, Any],
        *,
        connected: bool = True,
        schemas: dict[str, Any] | None = None,
    ) -> None:
        self.tools = tools
        self.connected = connected
        self.schemas = schemas or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        def done(
            code: int, out: Any = "", err: str = ""
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, code, out if isinstance(out, str) else json.dumps(out), err
            )

        if argv[1] == "connect":
            return done(0)
        if "tools-list" in argv:
            return (
                done(0, {"tools": []})
                if self.connected
                else done(1, err="unauthorized")
            )
        if "tools-get" in argv:
            return done(
                0,
                self.schemas.get(
                    argv[-1], {"name": argv[-1], "inputSchema": {"properties": {}}}
                ),
            )
        tool, arguments = argv[-2], json.loads(argv[-1])
        self.calls.append((tool, arguments))
        answer = self.tools[tool]
        value = answer(arguments) if callable(answer) else answer
        if isinstance(value, Exception):
            return done(
                0, {"isError": True, "content": [{"type": "text", "text": str(value)}]}
            )
        text = value if isinstance(value, str) else json.dumps(value)
        return done(0, {"content": [{"type": "text", "text": text}]})


def plaud_transcript_pages(arguments: dict[str, Any]) -> Any:
    segments = json.loads((FIXTURES / "sources/plaud-0a1b2c3d4e5f.json").read_text())[
        "segments"
    ]
    if arguments["file_id"] == "of_99aa88bb77cc":
        raise AssertionError("an excluded recording must not be fetched")
    start = int(arguments.get("cursor", "0"))
    page = segments[start : start + 4]
    nxt = start + len(page)
    return {
        "file_id": arguments["file_id"],
        "block": "transaction",
        "total": len(segments),
        "offset": start,
        "limit": 500,
        "returned": len(page),
        "next_cursor": str(nxt),
        "segments": page,
    }


def plaud_tools(**overrides: Any) -> dict[str, Any]:
    tools: dict[str, Any] = {
        "list_files": (FIXTURES / "plaud-list-result.txt").read_text(),
        "get_transcript": plaud_transcript_pages,
        "get_file": {
            "id": "of_0a1b2c3d4e5f",
            "name": "Walk with Sam about the apiary",
            "presigned_url": "https://signed.example/audio.mp3?sig=secret",
            "duration": 412000,
            "source_list": [],
            "note_list": [],
        },
    }
    tools.update(overrides)
    return tools


def connector(name: str, fake: FakeMcpc) -> fetchers.Connector:
    return fetchers.Connector(name, runner=fake)


def test_unwrap_plauds_wrapped_list_result() -> None:
    value = fetchers.unwrap_tool_text((FIXTURES / "plaud-list-result.txt").read_text())
    assert value["type"] == "list" and [f["id"] for f in value["data"]] == [
        "of_0a1b2c3d4e5f",
        "of_99aa88bb77cc",
    ]


def test_fetch_plaud_saves_the_whole_transcript_and_respects_exclusions(
    tmp_path: Path,
) -> None:
    fake = FakeMcpc(plaud_tools())
    report = fetchers.fetch_plaud(
        tmp_path, connector=connector("plaud", fake), exclude_title="therapy"
    )
    assert report["saved"] == ["of_0a1b2c3d4e5f"] and report["excluded"] == [
        "of_99aa88bb77cc"
    ]
    saved = json.loads((tmp_path / "plaud-0a1b2c3d4e5f.json").read_text())
    assert len(saved["segments"]) == 6, "both transcript pages are joined"
    assert saved["url"] == "https://web.plaud.ai/file/0a1b2c3d4e5f"
    assert "presigned_url" not in json.dumps(saved), (
        "the signed audio link is never stored"
    )
    record = ls.attribute(
        ls.read_file(tmp_path / "plaud-0a1b2c3d4e5f.json", tmp_path), ["Robin Vale"]
    )
    assert record["kind"] == "Plaud recording" and len(record["turns"]) == 3
    # A second run fetches nothing again.
    fake.calls.clear()
    again = fetchers.fetch_plaud(
        tmp_path, connector=connector("plaud", fake), exclude_title="therapy"
    )
    assert again["already_saved"] == ["of_0a1b2c3d4e5f"]
    assert [c[0] for c in fake.calls] == ["list_files"]


def test_fetch_plaud_lists_recordings_with_no_transcript(tmp_path: Path) -> None:
    empty = {"segments": [], "total": 0, "returned": 0}
    fake = FakeMcpc(plaud_tools(get_transcript=empty))
    report = fetchers.fetch_plaud(
        tmp_path, connector=connector("plaud", fake), exclude_title="therapy"
    )
    assert report["no_transcript"] == ["of_0a1b2c3d4e5f"]
    assert not list(tmp_path.glob("plaud-*"))


def test_fetch_plaud_can_save_audio_for_transcription(tmp_path: Path) -> None:
    fake = FakeMcpc(plaud_tools(get_transcript={"segments": [], "total": 0}))
    fetched: list[str] = []

    def download(url: str, path: Path) -> None:
        fetched.append(url)
        path.write_bytes(b"audio")

    report = fetchers.fetch_plaud(
        tmp_path,
        connector=connector("plaud", fake),
        exclude_title="therapy",
        audio_without_transcript=True,
        download=download,
    )
    assert report["audio_saved"] == ["of_0a1b2c3d4e5f"]
    assert (tmp_path / "plaud-0a1b2c3d4e5f.mp3").read_bytes() == b"audio"


def test_one_failing_recording_does_not_stop_the_rest(tmp_path: Path) -> None:
    listing = {
        "type": "list",
        "data": [
            {"id": "of_aa", "name": "one"},
            {"id": "of_0a1b2c3d4e5f", "name": "two"},
        ],
        "page": 1,
        "page_size": 100,
    }

    def transcript(arguments: dict[str, Any]) -> Any:
        if arguments["file_id"] == "of_aa":
            return RuntimeError("server error")
        return plaud_transcript_pages(arguments)

    report = fetchers.fetch_plaud(
        tmp_path,
        connector=connector(
            "plaud",
            FakeMcpc(plaud_tools(list_files=listing, get_transcript=transcript)),
        ),
    )
    assert report["saved"] == ["of_0a1b2c3d4e5f"]
    assert (
        report["failed"][0]["id"] == "of_aa"
        and "server error" in report["failed"][0]["error"]
    )


def test_plaud_listing_pages_until_a_short_page(tmp_path: Path) -> None:
    def listing(arguments: dict[str, Any]) -> Any:
        page = arguments["page"]
        count = 100 if page == 1 else 3
        return {
            "type": "list",
            "data": [{"id": f"of_{page}_{i}", "name": "x"} for i in range(count)],
        }

    files = fetchers.list_plaud_files(
        connector("plaud", FakeMcpc({"list_files": listing})),
        since="2026-01-01",
        until=None,
        limit=None,
    )
    assert len(files) == 103


def test_a_missing_sign_in_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(fetchers.FetchError, match="sign in once"):
        fetchers.fetch_plaud(
            tmp_path, connector=connector("plaud", FakeMcpc({}, connected=False))
        )


def test_fetch_otter_through_search_and_fetch(tmp_path: Path) -> None:
    schemas = {
        "search": {
            "name": "search",
            "inputSchema": {
                "properties": {
                    "query": {"type": "string"},
                    "start_date": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        "fetch": {
            "name": "fetch",
            "inputSchema": {
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    }
    results = {
        "results": [
            {
                "id": "AbCdEf123",
                "title": "Garden committee check-in",
                "url": "https://otter.ai/u/AbCdEf123",
            },
            {"id": "Private1", "title": "Private: doctor"},
        ]
    }
    fake = FakeMcpc(
        {
            "search": results,
            "fetch": json.loads((FIXTURES / "otter-fetch-result.json").read_text()),
        },
        schemas=schemas,
    )
    report = fetchers.fetch_otter(
        tmp_path,
        connector=connector("otter", fake),
        since="2026-01-01",
        exclude_title="^private",
    )
    assert report["saved"] == ["AbCdEf123"] and report["excluded"] == ["Private1"]
    assert fake.calls[0] == ("search", {"query": "", "start_date": "2026-01-01"})
    assert fake.calls[1] == ("fetch", {"id": "AbCdEf123"})
    record = ls.attribute(
        ls.read_file(tmp_path / "otter-AbCdEf123.json", tmp_path), ["Robin Vale"]
    )
    assert record["title"] == "Garden committee check-in" and len(record["turns"]) == 2


def test_otter_turn_lists_are_read_too() -> None:
    fetched = {
        "title": "Call",
        "transcript": [
            {
                "speaker_name": "Robin Vale",
                "start_offset": 3000,
                "text": "Hello there friend.",
            },
            {"speaker": {"name": "Jordan"}, "start": "0:07", "text": "Hi."},
        ],
    }
    record = fetchers.otter_record("X1", {}, fetched)
    assert record["segments"] == [
        {"start_ms": 3000, "speaker": "Robin Vale", "text": "Hello there friend."},
        {"start_ms": 7000, "speaker": "Jordan", "text": "Hi."},
    ]
    assert record["raw"] == fetched


def test_otter_ids_from_urls() -> None:
    assert (
        fetchers.otter_id({"url": "https://otter.ai/u/NpsJNrxvrc?view=summary"})
        == "NpsJNrxvrc"
    )
    assert fetchers.otter_id({"otid": "abc"}) == "abc"
    assert fetchers.otter_fetch_arguments(
        {"inputSchema": {"properties": {"url": {}}, "required": ["url"]}}, "abc"
    ) == {"url": "https://otter.ai/u/abc"}


def test_fetch_substack_saves_posts_and_flags_paid_ones(tmp_path: Path) -> None:
    post = json.loads((FIXTURES / "substack-post.json").read_text())
    pages = {
        0: [
            {"slug": "the-public-shelf", "type": "newsletter"},
            {"slug": "paid-one", "type": "newsletter"},
        ],
        2: [],
    }
    paid = {
        **post,
        "slug": "paid-one",
        "title": "Paid",
        "audience": "only_paid",
        "body_html": "<p>Teaser</p>",
    }

    def http_get(url: str) -> Any:
        if "/api/v1/archive" in url:
            return pages[int(url.split("offset=")[1].split("&")[0])]
        return post if url.endswith("the-public-shelf") else paid

    report = fetchers.fetch_substack("robinvale", tmp_path, http_get=http_get)
    assert sorted(report["saved"]) == ["paid-one", "the-public-shelf"] and report[
        "partial"
    ] == ["paid-one"]
    record = ls.read_file(tmp_path / "substack-the-public-shelf.json", tmp_path)
    assert record["url"] == "https://robinvale.substack.com/p/the-public-shelf"
    again = fetchers.fetch_substack("robinvale", tmp_path, http_get=http_get)
    assert sorted(again["already_saved"]) == ["paid-one", "the-public-shelf"]


@pytest.mark.parametrize(
    ("given", "base"),
    [
        ("robinvale", "https://robinvale.substack.com"),
        ("robinvale.substack.com/", "https://robinvale.substack.com"),
        ("https://www.example.com/archive", "https://www.example.com"),
    ],
)
def test_substack_base(given: str, base: str) -> None:
    assert fetchers.substack_base(given) == base


def test_transcribe_marks_its_model_cache_as_rebuildable(tmp_path: Path) -> None:
    import transcribe

    cache = transcribe.ensure_rebuildable_store_dir(tmp_path / "models")
    assert (cache / ".nobackup").exists()
    (cache / ".nobackup").unlink()
    transcribe.ensure_rebuildable_store_dir(cache)
    assert (cache / ".nobackup").exists()
    (tmp_path / "a.mp3").write_bytes(b"x")
    (tmp_path / "b.m4a").write_bytes(b"x")
    (tmp_path / "b.m4a.transcript.json").write_text("{}")
    assert [p.name for p in transcribe.pending_audio(tmp_path)] == ["a.mp3"]
