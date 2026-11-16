"""Fixture tests for the readers, the word-for-word quote check, and the model-reply parsing."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import library_model as lm  # noqa: E402
import library_sources as ls  # noqa: E402

FIXTURES = SCRIPTS.parent / "tests" / "fixtures"


def read(name: str, tmp_path: Path, authors: list[str] | None = None) -> dict:
    target = tmp_path / Path(name).name
    shutil.copy(FIXTURES / name, target)
    return ls.attribute(ls.read_file(target, tmp_path), authors or [])


def test_markdown_front_matter_sets_title_and_link(tmp_path: Path) -> None:
    record = read("sources/the-slow-hive.md", tmp_path)
    assert record["title"] == "The Slow Hive"
    assert record["url"] == "https://example.com/p/the-slow-hive"
    assert record["kind"] == "Writing"
    assert record["raw"].startswith("I kept bees for eleven years")
    assert "---" not in record["raw"] and record["segments"] is None


def test_plain_note_takes_its_first_line_as_title(tmp_path: Path) -> None:
    record = read("sources/garden-notes.txt", tmp_path)
    assert record["title"] == "Notes from the seed swap"
    assert record["url"] is None and record["status"] == "ok"


def test_plaud_transcript_keeps_only_the_authors_turns_with_times(
    tmp_path: Path,
) -> None:
    record = read("sources/plaud-0a1b2c3d4e5f.json", tmp_path, ["robin vale"])
    assert record["platform"] == "plaud" and record["kind"] == "Plaud recording"
    assert record["url"] == "https://web.plaud.ai/file/0a1b2c3d4e5f"
    assert [s["speaker"] for s in record["segments"]] == ["Sam Ortiz", "Robin Vale"] * 3
    assert record["segments"][1]["start_ms"] == 6900
    assert record["raw"].startswith("[0:06] Robin Vale\nFive again.")
    assert "Sam Ortiz" not in record["raw"]
    assert "Sam Ortiz: So how many hives" in record["text"]
    assert [t["start_ms"] for t in record["turns"]] == [6900, 26500, 58500]
    for turn in record["turns"]:
        assert record["raw"][turn["start"] : turn["end"]] in record["text"]


def test_a_you_suffix_on_the_speaker_label_still_matches_the_author(
    tmp_path: Path,
) -> None:
    data = json.loads((FIXTURES / "sources/plaud-0a1b2c3d4e5f.json").read_text())
    for segment in data["segments"]:
        if segment["speaker"] == "Robin Vale":
            segment["speaker"] = "Robin Vale (You)"
    (tmp_path / "p.json").write_text(json.dumps(data))
    record = ls.attribute(ls.read_file(tmp_path / "p.json", tmp_path), ["Robin Vale"])
    assert len(record["turns"]) == 3


def test_whisper_voice_note_is_all_the_authors_and_links_its_audio(
    tmp_path: Path,
) -> None:
    shutil.copy(FIXTURES / "sources/voice-note-shelf.mp3", tmp_path)
    record = read("sources/voice-note-shelf.mp3.transcript.json", tmp_path)
    assert record["kind"] == "Voice note" and record["platform"] == "whisper"
    assert record["audio"] == "voice-note-shelf.mp3"
    assert record["segments"][1]["start_ms"] == 9800
    assert record["raw"].startswith("Quick note. Write something")
    assert record["transcription_note"] == ls.AUTOMATIC_TRANSCRIPT_NOTE


def test_otter_text_export_is_read_as_a_transcript(tmp_path: Path) -> None:
    record = read("otter-export.txt", tmp_path, ["Robin Vale"])
    assert record["kind"] == "Transcript"
    assert [(s["speaker"], s["start_ms"]) for s in record["segments"]] == [
        ("Robin Vale", 3000),
        ("Jordan Lee", 11000),
        ("Robin Vale", 15000),
    ]
    assert "Some things need permission" not in record["raw"]
    assert "experiments are cheap" in record["raw"]


def test_bracketed_transcript_lines_are_read(tmp_path: Path) -> None:
    (tmp_path / "t.txt").write_text(
        "[00:05] Robin Vale: First thought here.\n[00:09] Jordan: A reply.\n[01:02:03] Robin Vale: Later on.\n"
    )
    record = ls.attribute(ls.read_file(tmp_path / "t.txt", tmp_path), ["Robin Vale"])
    assert [s["start_ms"] for s in record["segments"]] == [5000, 9000, 3723000]
    assert (
        record["raw"]
        == "[0:05] Robin Vale\nFirst thought here.\n\n[1:02:03] Robin Vale\nLater on."
    )


def test_prose_with_a_few_times_in_it_is_not_mistaken_for_a_transcript(
    tmp_path: Path,
) -> None:
    (tmp_path / "p.txt").write_text(
        "We met at 10:30 for coffee.\nThe talk was long and the room was cold.\nThen we went home.\n"
    )
    assert ls.read_file(tmp_path / "p.txt", tmp_path)["segments"] is None


def test_substack_post_becomes_text_without_scripts(tmp_path: Path) -> None:
    record = read("substack-post.json", tmp_path)
    assert (
        record["kind"] == "Substack article" and record["title"] == "The Public Shelf"
    )
    assert record["url"] == "https://robinvale.substack.com/p/the-public-shelf"
    assert record["raw"] == (
        "What a box of zucchini taught me about trust\n\nEvery August the zucchini arrive all at once.\n\n"
        "A shelf at the gate turned a nuisance into a gift."
    )
    assert "track()" not in record["raw"]


def test_otter_connector_result_with_text_is_read(tmp_path: Path) -> None:
    import fetchers

    fetched = json.loads((FIXTURES / "otter-fetch-result.json").read_text())
    record = fetchers.otter_record("AbCdEf123", {}, fetched)
    (tmp_path / "otter-AbCdEf123.json").write_text(json.dumps(record))
    source = ls.attribute(
        ls.read_file(tmp_path / "otter-AbCdEf123.json", tmp_path), ["Robin Vale"]
    )
    assert source["kind"] == "Otter conversation"
    assert source["url"] == "https://otter.ai/u/AbCdEf123"
    assert source["date"] == "2026-06-01T18:00:00Z"
    assert len(source["turns"]) == 2 and "Jordan" not in source["raw"]


def test_vtt_captions_with_voice_tags(tmp_path: Path) -> None:
    record = read("captions.vtt", tmp_path)
    assert record["kind"] == "Video captions"
    assert [s["text"] for s in record["segments"]] == [
        "Welcome back to the garden.",
        "Today: why the best tools are the ones you share.",
    ]
    assert record["segments"][1]["start_ms"] == 4500
    assert record["status"] == "ok"


def test_srt_captions(tmp_path: Path) -> None:
    (tmp_path / "c.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n2\n00:00:02,500 --> 00:00:04,000\nSecond line of words here.\n"
    )
    record = ls.read_file(tmp_path / "c.srt", tmp_path)
    assert [s["start_ms"] for s in record["segments"]] == [1000, 2500]


def test_html_article_title_and_canonical_link(tmp_path: Path) -> None:
    (tmp_path / "a.html").write_text(
        '<html><head><title>Site</title><link rel="canonical" href="https://example.com/a"></head>'
        "<body><h1>Lending Libraries</h1><p>Tools are better shared.</p><style>p{}</style></body></html>"
    )
    record = ls.read_file(tmp_path / "a.html", tmp_path)
    assert (
        record["title"] == "Lending Libraries"
        and record["url"] == "https://example.com/a"
    )
    assert record["text"] == "Lending Libraries\n\nTools are better shared."


def test_sidecar_metadata_overrides(tmp_path: Path) -> None:
    (tmp_path / "n.txt").write_text("Some words that make a note of reasonable length.")
    (tmp_path / "n.txt.source.json").write_text(
        json.dumps(
            {"title": "Named", "url": "https://example.com/n", "kind": "Journal"}
        )
    )
    record = ls.read_file(tmp_path / "n.txt", tmp_path)
    assert (record["title"], record["url"], record["kind"]) == (
        "Named",
        "https://example.com/n",
        "Journal",
    )


@pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="pdftotext is not installed"
)
def test_text_pdf_is_read(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_tiny_pdf("A shared shelf is infrastructure for trust."))
    record = ls.read_file(pdf, tmp_path)
    assert "A shared shelf is infrastructure for trust." in record["text"]


def _tiny_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_discover_sets_aside_unsupported_and_untranscribed_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.md").write_text("words")
    (tmp_path / "b.docx").write_bytes(b"x")
    (tmp_path / "c.m4a").write_bytes(b"x")
    (tmp_path / ".hidden.md").write_text("x")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "d.md").write_text("x")
    readable, skipped = ls.discover(tmp_path, ["private/*"])
    assert [p.name for p in readable] == ["a.md"]
    reasons = {s["path"]: s["reason"] for s in skipped}
    assert reasons["b.docx"].startswith("unsupported")
    assert "transcribe" in reasons["c.m4a"]
    assert reasons["private/d.md"] == "excluded"


def test_ids_are_stable_and_avoid_the_librarys_own(tmp_path: Path) -> None:
    records = [
        {"path": "notes/Garden Notes.txt", "id": ""},
        {"path": "garden-notes.md", "id": ""},
    ]
    taken: dict[str, str] = {}
    ls.assign_ids(records, taken, {"notes-garden-notes"})
    assert (
        records[0]["id"] == "notes-garden-notes-2"
        and records[1]["id"] == "garden-notes"
    )
    again = [{"path": "notes/Garden Notes.txt", "id": ""}]
    ls.assign_ids(again, taken)
    assert again[0]["id"] == "notes-garden-notes-2"


# ---- quotes ------------------------------------------------------------------

RAW = 'She said: "Rest is part of the work" — and meant it.\n\nThe tide comes back   every evening.'


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        (
            "The tide comes back   every evening.",
            "The tide comes back   every evening.",
        ),
        ("The tide comes back every evening.", "The tide comes back   every evening."),
        (
            "“Rest is part of the work” - and meant it.",
            '"Rest is part of the work" — and meant it.',
        ),
        ("...comes back every evening", "comes back   every evening"),
        ("The tide always comes back every evening.", None),
        ("tide", None),
        ("[0:06] Robin Vale The tide", None),
    ],
)
def test_locate_quote(quote: str, expected: str | None) -> None:
    assert lm.locate_quote(quote, RAW) == expected


def test_a_quote_spanning_two_transcript_turns_is_refused(tmp_path: Path) -> None:
    record = read("sources/plaud-0a1b2c3d4e5f.json", tmp_path, ["Robin Vale"])
    spanning = (
        "spends its energy on the colony, not on repairing what I disturbed. Maybe."
    )
    assert lm.evidence_for(record, spanning) is None


def test_evidence_in_a_later_turn_gets_that_turns_time(tmp_path: Path) -> None:
    record = read("sources/plaud-0a1b2c3d4e5f.json", tmp_path, ["Robin Vale"])
    entry = lm.evidence_for(record, "Trust is something you prepare for.")
    assert entry == {
        "source_id": record["id"],
        "quote": "Trust is something you prepare for.",
        "start_ms": 26500,
        "timestamp": "0:26",
    }


def test_parse_json_reply_tolerates_fences_and_prose() -> None:
    assert lm.parse_json_reply('```json\n{"ideas": []}\n```') == {"ideas": []}
    assert lm.parse_json_reply('Here you go: {"a": 1} hope that helps') == {"a": 1}
    with pytest.raises(ValueError):
        lm.parse_json_reply("no json here")


def test_long_sources_are_split_at_paragraphs() -> None:
    raw = "\n\n".join(" ".join(["word"] * 400) for _ in range(5))
    chunks = lm.chunk_text(raw, limit=1000)
    assert [len(c.split()) for c in chunks] == [800, 800, 400]
    assert "\n\n".join(chunks) == raw


def test_organize_falls_back_when_the_model_skips_an_idea() -> None:
    def model(prompt: str, system: str) -> tuple[str, float]:
        return json.dumps(
            {
                "chapters": [{"name": "Care", "subtitle": "looking after"}],
                "placements": [
                    {"cid": "a", "chapter": "Care"},
                    {"cid": "b", "chapter": "Nonexistent"},
                ],
            }
        ), 0.0

    result = lm.organize(
        [],
        [],
        [
            {"cid": "a", "title": "A", "summary": "a"},
            {"cid": "b", "title": "B", "summary": "b"},
        ],
        model,
    )
    assert result["placements"] == [
        {"cid": "a", "chapter": "Care"},
        {"cid": "b", "chapter": lm.FALLBACK_CHAPTER},
    ]
    assert [c["name"] for c in result["chapters"]] == ["Care", lm.FALLBACK_CHAPTER]


def test_organize_never_renames_existing_chapters() -> None:
    def model(prompt: str, system: str) -> tuple[str, float]:
        return json.dumps(
            {
                "chapters": [{"name": "Renamed", "subtitle": "x"}],
                "placements": [{"cid": "a", "merge_into": "old-idea"}],
            }
        ), 0.0

    existing = [
        {"id": "old-idea", "title": "Old", "summary": "old", "group": "Original"}
    ]
    result = lm.organize(
        [{"name": "Original", "subtitle": "first"}],
        existing,
        [{"cid": "a", "title": "A", "summary": "a"}],
        model,
    )
    assert result["chapters"] == [{"name": "Original", "subtitle": "first"}]
    assert result["placements"] == [{"cid": "a", "merge_into": "old-idea"}]
