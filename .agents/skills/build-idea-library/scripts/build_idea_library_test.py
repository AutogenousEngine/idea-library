"""End-to-end tests of the build-idea-library steps, with a scripted stand-in for the model.

The stand-in answers like the model would -- ideas with quotes, chapter placements,
connections -- and can be told to misbehave (paraphrase a quote, claim an expressed link
with no real quote) so the checks that keep the library honest are exercised.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import library_build as lb  # noqa: E402
import library_model as lm  # noqa: E402
import run as runner  # noqa: E402
from library_store import KEEP_BACKUPS, KEEP_RUNS  # noqa: E402

FIXTURES = SCRIPTS.parent / "tests" / "fixtures"
APP_FIXTURE = (
    SCRIPTS.parents[3]
    / "system"
    / "apps"
    / "idea-library"
    / "testdata"
    / "map-data.json"
)


class FakeModel:
    """Answers each model step from the prompt it is given."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.paraphrase = False
        self.fake_expressed = False
        self.chapters = {
            "hive": "Care and restraint",
            "shelf": "Shared abundance",
            "seed": "Shared abundance",
        }

    def __call__(self, prompt: str, system: str) -> tuple[str, float]:
        if system == lm.EXTRACT_SYSTEM:
            self.calls.append("extract")
            return self.extract(prompt), 0.01
        if system == lm.ORGANIZE_SYSTEM:
            self.calls.append("organize")
            return self.organize(prompt), 0.02
        self.calls.append("connect")
        return self.connect(prompt), 0.01

    def extract(self, prompt: str) -> str:
        source = prompt.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
        sentences = [
            s
            for s in re.split(
                r"(?<=[.!?])\s+", re.sub(r"\[\d+:\d+\] [^\n]+\n", "", source)
            )
            if len(s) > 30
        ]
        ideas = []
        for n, sentence in enumerate(sentences[:2]):
            quote = sentence.strip()
            if self.paraphrase and n == 1:
                quote = "This is a paraphrase that appears nowhere in the source text at all."
            ideas.append(
                {
                    "title": " ".join(sentence.split()[:4]).strip(".,"),
                    "summary": f"The author holds that {sentence.strip().lower()}",
                    "quotes": [quote.replace("'", "’")],
                    "writing_seed": "A short post about it.",
                    "formats": ["Twitter post", "Not a format"],
                }
            )
        return "```json\n" + json.dumps({"ideas": ideas}) + "\n```"

    def organize(self, prompt: str) -> str:
        new = json.loads(
            prompt.split("New ideas to place (cid, title, summary):\n", 1)[1].split(
                "\n\nFor every", 1
            )[0]
        )
        placements = []
        seen_titles: dict[str, str] = {}
        for item in new:
            text = (item["title"] + " " + item["summary"]).lower()
            if item["title"] in seen_titles:
                placements.append(
                    {"cid": item["cid"], "merge_into": seen_titles[item["title"]]}
                )
                continue
            seen_titles[item["title"]] = item["cid"]
            chapter = next(
                (c for k, c in self.chapters.items() if k in text), "Ways of working"
            )
            placements.append({"cid": item["cid"], "chapter": chapter})
        names = sorted({p["chapter"] for p in placements if "chapter" in p})
        return json.dumps(
            {
                "collection": "Notes from the Garden Gate",
                "reading_intro": "Start with care, end with what we share.",
                "chapters": [
                    {"name": n, "subtitle": f"about {n.lower()}"} for n in names
                ],
                "placements": placements,
            }
        )

    def connect(self, prompt: str) -> str:
        if "SOURCE:\n<<<" in prompt:
            cards = json.loads(prompt.split("):\n", 1)[1].split("\n\nPropose", 1)[0])
            match = re.search(r'source_id "([^"]+)"', prompt)
            assert match is not None
            source_id = match.group(1)
            raw = prompt.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
            first_sentence = re.split(
                r"(?<=[.!?])\s+", re.sub(r"\[\d+:\d+\] [^\n]+\n", "", raw)
            )[0]
            if len(cards) < 2:
                return json.dumps({"connections": []})
            quote = (
                "Words that are not in the source anywhere at all."
                if self.fake_expressed
                else first_sentence
            )
            return json.dumps(
                {
                    "connections": [
                        {
                            "source": cards[0]["id"],
                            "target": cards[1]["id"],
                            "label": "leads to",
                            "basis": "expressed",
                            "why": "The author links them in one passage.",
                            "quotes": [{"source_id": source_id, "quote": quote}],
                        }
                    ]
                }
            )
        cards = json.loads(
            prompt.split("with their source_id):\n", 1)[1].split("\n\nPropose", 1)[0]
        )
        new_ids = json.loads(
            prompt.split("any other idea in the library:\n", 1)[1].split("\n", 1)[0]
        )
        others = [c["id"] for c in cards if c["id"] not in new_ids]
        target = (
            others[0]
            if others
            else (cards[-1]["id"] if cards[-1]["id"] != new_ids[0] else None)
        )
        if not target:
            return json.dumps({"connections": []})
        return json.dumps(
            {
                "connections": [
                    {
                        "source": new_ids[0],
                        "target": target,
                        "label": "may help explain",
                        "basis": "suggested",
                        "why": "An interpretation: both concern trust.",
                        "quotes": [],
                    }
                ]
            }
        )


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    sources = tmp_path / "sources"
    shutil.copytree(FIXTURES / "sources", sources)
    return {
        "sources": sources,
        "state": tmp_path / "state",
        "library": tmp_path / "library",
    }


def cli(ws: dict[str, Path], *argv: str) -> list[str]:
    return [
        *argv,
        "--sources",
        str(ws["sources"]),
        "--state-dir",
        str(ws["state"]),
        "--library-dir",
        str(ws["library"]),
    ]


def library(ws: dict[str, Path]) -> dict[str, Any]:
    return json.loads((ws["library"] / "map-data.json").read_text(encoding="utf-8"))


def all_quotes_verbatim(data: dict[str, Any]) -> bool:
    raw = {s["id"]: s["raw"] for s in data["sources"]}
    return all(
        e["quote"] in raw[e["source_id"]]
        for item in data["nodes"] + data["edges"]
        for e in item.get("evidence", [])
    )


def test_a_new_library_is_built_in_the_app_shape(workspace: dict[str, Path]) -> None:
    model = FakeModel()
    runner.run(
        cli(workspace, "all", "--author", "Robin Vale", "--display-name", "Robin"),
        complete=model,
    )
    data = library(workspace)
    errors, warnings = lb.validate_library(data, strict_quotes=True)
    assert errors == [] and warnings == []
    assert data["author"] == "Robin"
    assert {c["name"] for c in data["chapters"]} == {n["group"] for n in data["nodes"]}
    assert all(c["subtitle"] for c in data["chapters"])
    assert all_quotes_verbatim(data)
    # Curly apostrophes the model introduced were matched back to the source's own wording.
    assert not any("’" in e["quote"] for n in data["nodes"] for e in n["evidence"])
    sources = {s["id"]: s for s in data["sources"]}
    plaud = sources["plaud-0a1b2c3d4e5f"]
    assert plaud["url"] == "https://web.plaud.ai/file/0a1b2c3d4e5f"
    assert "Sam Ortiz" not in plaud["raw"] and "Robin Vale" in plaud["raw"]
    assert plaud["transcript"] == "map-assets/transcripts/plaud-0a1b2c3d4e5f.txt"
    assert "Sam Ortiz" in (workspace["library"] / plaud["transcript"]).read_text()
    assert "labelled as Robin Vale" in plaud["transcription_note"]
    plaud_quotes = [
        e
        for n in data["nodes"]
        for e in n["evidence"]
        if e["source_id"] == "plaud-0a1b2c3d4e5f"
    ]
    assert plaud_quotes and all(
        re.fullmatch(r"\d+:\d{2}", e["timestamp"]) and isinstance(e["start_ms"], int)
        for e in plaud_quotes
    )
    # A source with no web link points at its saved copy, which the app serves.
    notes = sources["garden-notes"]
    assert notes["url"] == "map-assets/sources/garden-notes.txt"
    assert (
        (workspace["library"] / notes["url"])
        .read_text()
        .startswith("Notes from the seed swap")
    )
    voice = sources["voice-note-shelf"]
    assert (
        voice["kind"] == "Voice note"
        and voice["audio"] == "map-assets/audio/voice-note-shelf.mp3"
    )
    assert (
        workspace["library"] / voice["audio"]
    ).read_bytes() == b"ID3fake-audio-bytes"
    assert sources["the-slow-hive"]["url"] == "https://example.com/p/the-slow-hive"
    expressed = [e for e in data["edges"] if e["basis"] == "expressed"]
    suggested = [e for e in data["edges"] if e["basis"] == "suggested"]
    assert expressed and all(e["evidence"] for e in expressed)
    assert suggested
    assert len({e["id"] for e in data["edges"]}) == len(data["edges"])
    # Formats outside the known list never reach the classic view.
    sample = json.loads((workspace["library"] / "sample.json").read_text())
    assert sample["generated_by"] == "build-idea-library"
    assert {i["id"] for i in sample["ideas"]} == {n["id"] for n in data["nodes"]}
    assert all(set(i["formats"]) <= set(lm.FORMATS) for i in sample["ideas"])
    corpus = (workspace["library"] / "corpus.html").read_text()
    assert lb.GENERATED_MARKER in corpus and "Walk with Sam about the apiary" in corpus
    assert "__CORPUS_DATA__" not in corpus


def test_nodes_are_laid_out_per_chapter_without_overlap(
    workspace: dict[str, Path],
) -> None:
    runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=FakeModel())
    data = library(workspace)
    positions = [(n["x"], n["y"]) for n in data["nodes"]]
    assert len(set(positions)) == len(positions)
    regions = {}
    for node in data["nodes"]:
        regions.setdefault(node["group"], set()).add(
            (node["x"] - lb.ORIGIN_X) // lb.REGION_WIDTH
        )
    # Every chapter's ideas sit in one region of the map.
    assert all(len(columns) == 1 for columns in regions.values())


def test_a_paraphrased_quote_is_dropped_and_its_idea_with_it(
    workspace: dict[str, Path],
) -> None:
    model = FakeModel()
    model.paraphrase = True
    result = runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=model)
    assert result["extract"]["dropped_quotes"] >= 1
    assert result["extract"]["dropped_ideas"] >= 1
    data = library(workspace)
    assert all_quotes_verbatim(data)
    assert not any(
        "paraphrase" in e["quote"] for n in data["nodes"] for e in n["evidence"]
    )


def test_an_expressed_link_without_a_real_quote_becomes_suggested(
    workspace: dict[str, Path],
) -> None:
    model = FakeModel()
    model.fake_expressed = True
    result = runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=model)
    assert result["connect"]["downgraded"] >= 1
    data = library(workspace)
    assert not any(e["basis"] == "expressed" for e in data["edges"])
    assert all_quotes_verbatim(data)


def test_rerunning_extends_the_library_and_keeps_hand_edits(
    workspace: dict[str, Path],
) -> None:
    model = FakeModel()
    runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=model)
    first = library(workspace)
    # The user edits an idea and a connection by hand.
    first["nodes"][0]["title"] = "Hand-edited title"
    first["edges"][0]["why"] = "Hand-edited reason."
    (workspace["library"] / "map-data.json").write_text(json.dumps(first))
    extract_calls = model.calls.count("extract")
    # One new essay, one edited note.
    (workspace["sources"] / "new-essay.md").write_text(
        "# Tools in common\n\nA tool library lends drills the way a seed swap lends tomatoes. Ownership is often just storage we pay for.\n"
    )
    notes = workspace["sources"] / "garden-notes.txt"
    notes.write_text(
        notes.read_text().replace(
            "The seed swap worked because nobody was in charge of it.",
            "The swap ran itself.",
        )
    )
    # The settings from the first run are remembered: no --author needed.
    result = runner.run(cli(workspace, "all"), complete=model)
    assert model.calls.count("extract") - extract_calls == 2
    assert (
        result["ingest"]["new"] == 1
        and result["ingest"]["changed"] == 1
        and result["ingest"]["unchanged"] == 3
    )
    data = library(workspace)
    titles = {n["title"] for n in data["nodes"]}
    assert "Hand-edited title" in titles
    assert any(e["why"] == "Hand-edited reason." for e in data["edges"])
    assert any(s["id"] == "new-essay" for s in data["sources"])
    assert result["build"]["refresh"]["stale_quotes"], (
        "a quote of the changed note no longer in it is reported"
    )
    assert all_quotes_verbatim(data)
    assert lb.validate_library(data, strict_quotes=True)[0] == []
    backups = list((workspace["state"] / "backups").glob("map-data-*.json"))
    assert len(backups) == 1


def test_a_rerun_with_nothing_new_makes_no_model_calls(
    workspace: dict[str, Path],
) -> None:
    model = FakeModel()
    runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=model)
    before = library(workspace)
    calls = len(model.calls)
    runner.run(cli(workspace, "all"), complete=model)
    assert len(model.calls) == calls
    after = library(workspace)
    assert after == before, "nothing new: the library file is not rewritten"
    assert not (workspace["state"] / "backups").exists() or not list(
        (workspace["state"] / "backups").glob("*.json")
    )


def test_a_transcript_where_the_author_never_speaks_is_set_aside(
    workspace: dict[str, Path],
) -> None:
    result = runner.run(
        cli(workspace, "ingest", "--author", "Somebody Else"), complete=FakeModel()
    )
    reasons = {item["path"]: item["reason"] for item in result["set_aside"]}
    assert "never speaks" in reasons["plaud-0a1b2c3d4e5f.json"]
    # The single-voice note has no speaker labels, so it stays the author's.
    assert "voice-note-shelf.mp3.transcript.json" not in reasons


def test_several_speakers_and_no_author_named_is_set_aside(
    workspace: dict[str, Path],
) -> None:
    result = runner.run(cli(workspace, "ingest"), complete=FakeModel())
    reasons = {item["path"]: item["reason"] for item in result["set_aside"]}
    assert "no author named" in reasons["plaud-0a1b2c3d4e5f.json"]


def test_excluded_files_never_reach_the_library(workspace: dict[str, Path]) -> None:
    runner.run(
        cli(workspace, "all", "--author", "Robin Vale", "--exclude", "garden-*"),
        complete=FakeModel(),
    )
    data = library(workspace)
    assert "garden-notes" not in {s["id"] for s in data["sources"]}
    assert (
        "Notes from the seed swap"
        not in (workspace["library"] / "corpus.html").read_text()
    )


def test_an_empty_folder_fails_clearly_and_writes_nothing(tmp_path: Path) -> None:
    ws = {
        "sources": tmp_path / "sources",
        "state": tmp_path / "state",
        "library": tmp_path / "library",
    }
    ws["sources"].mkdir()
    with pytest.raises(runner.StepError, match="no readable sources"):
        runner.run(cli(ws, "all"), complete=FakeModel())
    assert not (ws["library"] / "map-data.json").exists()


def test_a_missing_folder_fails_clearly(tmp_path: Path) -> None:
    ws = {
        "sources": tmp_path / "nowhere",
        "state": tmp_path / "state",
        "library": tmp_path / "library",
    }
    with pytest.raises(runner.StepError, match="does not exist"):
        runner.run(cli(ws, "ingest"), complete=FakeModel())


def test_the_cost_ceiling_stops_before_any_model_call(
    workspace: dict[str, Path],
) -> None:
    model = FakeModel()
    with pytest.raises(runner.StepError, match="over --max-cost"):
        runner.run(
            cli(workspace, "all", "--author", "Robin Vale", "--max-cost", "0.001"),
            complete=model,
        )
    assert model.calls == []


def test_estimate_counts_only_sources_waiting_for_the_model(
    workspace: dict[str, Path],
) -> None:
    runner.run(cli(workspace, "ingest", "--author", "Robin Vale"), complete=FakeModel())
    before = runner.run(cli(workspace, "estimate"), complete=FakeModel())
    assert before["sources"] == 4 and before["estimated_cost_usd"] > 0
    runner.run(cli(workspace, "extract"), complete=FakeModel())
    after = runner.run(cli(workspace, "estimate"), complete=FakeModel())
    assert after["sources"] == 0 and after["estimated_cost_usd"] == 0.0


def test_steps_run_one_at_a_time_match_all(
    workspace: dict[str, Path], tmp_path: Path
) -> None:
    for step in ("ingest", "extract", "organize", "connect", "build"):
        argv = cli(workspace, step) + (
            ["--author", "Robin Vale"] if step == "ingest" else []
        )
        runner.run(argv, complete=FakeModel())
    stepwise = library(workspace)
    other = {
        "sources": workspace["sources"],
        "state": tmp_path / "state2",
        "library": tmp_path / "library2",
    }
    runner.run(cli(other, "all", "--author", "Robin Vale"), complete=FakeModel())
    together = library(other)
    assert [n["title"] for n in stepwise["nodes"]] == [
        n["title"] for n in together["nodes"]
    ]
    assert len(stepwise["edges"]) == len(together["edges"])


def test_build_refuses_ideas_sorted_before_new_material_arrived(
    workspace: dict[str, Path],
) -> None:
    runner.run(cli(workspace, "ingest", "--author", "Robin Vale"), complete=FakeModel())
    runner.run(cli(workspace, "extract"), complete=FakeModel())
    runner.run(cli(workspace, "organize"), complete=FakeModel())
    (workspace["sources"] / "late.md").write_text(
        "# Late\n\nA late essay about lending libraries and how sharing beats owning.\n"
    )
    runner.run(cli(workspace, "ingest"), complete=FakeModel())
    runner.run(cli(workspace, "extract"), complete=FakeModel())
    with pytest.raises(runner.StepError, match="run organize first"):
        runner.run(cli(workspace, "connect"), complete=FakeModel())


def test_an_existing_corpus_page_and_classic_sample_are_never_replaced(
    workspace: dict[str, Path],
) -> None:
    workspace["library"].mkdir(parents=True)
    (workspace["library"] / "corpus.html").write_text(
        "<html>the user's own corpus</html>"
    )
    (workspace["library"] / "sample.json").write_text('{"title": "hand made"}')
    result = runner.run(
        cli(workspace, "all", "--author", "Robin Vale"), complete=FakeModel()
    )
    assert result["build"]["companions"] == []
    assert (
        workspace["library"] / "corpus.html"
    ).read_text() == "<html>the user's own corpus</html>"
    assert json.loads((workspace["library"] / "sample.json").read_text()) == {
        "title": "hand made"
    }


def test_an_existing_hand_made_library_is_extended_not_replaced(
    workspace: dict[str, Path],
) -> None:
    workspace["library"].mkdir(parents=True)
    shutil.copy(APP_FIXTURE, workspace["library"] / "map-data.json")
    original = json.loads(APP_FIXTURE.read_text())
    runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=FakeModel())
    data = library(workspace)
    ids = {n["id"] for n in data["nodes"]}
    assert {n["id"] for n in original["nodes"]} <= ids
    for node in original["nodes"]:
        kept = next(n for n in data["nodes"] if n["id"] == node["id"])
        assert {k: kept[k] for k in ("title", "summary", "group", "evidence")} == {
            k: node[k] for k in ("title", "summary", "group", "evidence")
        }
    assert {s["id"] for s in original["sources"]} <= {s["id"] for s in data["sources"]}
    assert data["title"] == original["title"] and data["scope"] == original["scope"]
    assert len(data["nodes"]) > len(original["nodes"])
    # The fixture's own made-up quotes are not in its sources: a warning, never a refusal.
    errors, warnings = lb.validate_library(data, strict_quotes=False)
    assert errors == [] and warnings


def test_backups_and_run_reports_are_bounded(workspace: dict[str, Path]) -> None:
    runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=FakeModel())
    store = runner.Store(workspace["state"])
    library_file = workspace["library"] / "map-data.json"
    for _ in range(KEEP_BACKUPS + 3):
        store.backup_library(library_file)
    for n in range(KEEP_RUNS + 3):
        store.save_run_report({"n": n})
    assert (
        len(list((workspace["state"] / "backups").glob("map-data-*.json")))
        == KEEP_BACKUPS
    )
    reports = sorted((workspace["state"] / "runs").glob("run-*.json"))
    assert len(reports) == KEEP_RUNS
    assert json.loads(reports[-1].read_text()) == {"n": KEEP_RUNS + 2}


def test_a_changed_source_evicts_its_old_cached_ideas(
    workspace: dict[str, Path],
) -> None:
    runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=FakeModel())
    cached = list((workspace["state"] / "extractions").glob("garden-notes-*.json"))
    assert len(cached) == 1
    notes = workspace["sources"] / "garden-notes.txt"
    notes.write_text(
        notes.read_text()
        + "\nOne more thought about the shelf and how it keeps people honest.\n"
    )
    runner.run(cli(workspace, "all"), complete=FakeModel())
    now = list((workspace["state"] / "extractions").glob("garden-notes-*.json"))
    assert len(now) == 1 and now != cached


def test_copied_files_of_a_removed_source_are_pruned_but_others_are_left(
    workspace: dict[str, Path],
) -> None:
    workspace["library"].mkdir(parents=True)
    foreign = workspace["library"] / "map-assets" / "transcripts" / "someone-elses.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("not ours")
    runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=FakeModel())
    copy = workspace["library"] / "map-assets" / "sources" / "garden-notes.txt"
    assert copy.exists()
    (workspace["sources"] / "garden-notes.txt").unlink()
    store = runner.Store(workspace["state"])
    (store.sources_dir / "garden-notes.json").unlink()
    runner.run(cli(workspace, "all"), complete=FakeModel())
    assert not copy.exists()
    assert foreign.read_text() == "not ours"


def test_validate_command_reports_and_exits(
    workspace: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = runner.run(cli(workspace, "validate", str(APP_FIXTURE)))
    assert result["errors"] == []
    broken = json.loads(APP_FIXTURE.read_text())
    broken["edges"][0]["target"] = "missing-idea"
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken))
    with pytest.raises(SystemExit) as exit_info:
        runner.run(cli(workspace, "validate", str(path)))
    assert exit_info.value.code == 1
    assert "unknown idea 'missing-idea'" in capsys.readouterr().out


def test_the_library_names_its_owner_and_collection_from_the_material(
    workspace: dict[str, Path],
) -> None:
    runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=FakeModel())
    data = library(workspace)
    # With no display name given, the author's speaker name is the owner's name.
    assert data["author"] == "Robin Vale"
    assert data["collection"] == "Notes from the Garden Gate"
    assert data["reading_intro"] == "Start with care, end with what we share."
    assert [c["name"] for c in data["chapters"]] == list(
        dict.fromkeys(n["group"] for n in data["nodes"])
    )
    sample = json.loads((workspace["library"] / "sample.json").read_text())
    assert sample["author"] == "Robin Vale"
    assert sample["collection"] == "Notes from the Garden Gate"
    assert sample["note"] == data["scope"]


def test_hand_edited_owner_fields_survive_and_flags_replace_them(
    workspace: dict[str, Path],
) -> None:
    runner.run(cli(workspace, "all", "--author", "Robin Vale"), complete=FakeModel())
    data = library(workspace)
    data["collection"] = "My own name for it"
    data["reading_intro"] = "My own intro."
    (workspace["library"] / "map-data.json").write_text(json.dumps(data))
    (workspace["sources"] / "more.md").write_text(
        "# More\n\nA second shelf by the bus stop filled up in a week, which says something about the street.\n"
    )
    runner.run(cli(workspace, "all"), complete=FakeModel())
    data = library(workspace)
    assert data["collection"] == "My own name for it"
    assert data["reading_intro"] == "My own intro."
    runner.run(
        cli(
            workspace, "build", "--collection", "Garden Gate", "--display-name", "Robin"
        ),
        complete=FakeModel(),
    )
    data = library(workspace)
    assert (data["collection"], data["author"]) == ("Garden Gate", "Robin")


def test_validate_accepts_the_pages_optional_fields_and_rejects_wrong_types() -> None:
    data = json.loads(APP_FIXTURE.read_text())
    data.update(
        author="Robin",
        collection="Garden Gate",
        reading_intro="Read in order.",
        chapters=[{"name": data["nodes"][0]["group"], "subtitle": "x"}],
    )
    data["sources"][0]["link_label"] = "Read the essay"
    errors, _ = lb.validate_library(data, strict_quotes=False)
    assert errors == []
    data["collection"] = ["not", "text"]
    data["sources"][0]["link_label"] = 3
    errors, _ = lb.validate_library(data, strict_quotes=False)
    assert "'collection' must be text" in errors
    assert "source 'essay-tides' 'link_label' must be text or null" in errors
