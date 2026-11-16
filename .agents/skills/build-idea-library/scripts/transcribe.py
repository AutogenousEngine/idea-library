# /// script
# requires-python = ">=3.11"
# dependencies = ["faster-whisper==1.2.1", "av==15.1.0"]
# ///
"""Transcribe audio files in the sources folder that have no transcript yet, on this machine.

Writes ``<audio file>.transcript.json`` beside each recording (whisper's segments with
their timings and confidence, the model used, and the audio file's name), which the
ingest step reads as a voice note linked to its audio. A recording with no speech still
gets a file (with no segments), so it is not transcribed again.

The speech model is downloaded once into ``data/.state/models/faster-whisper/``, marked
``.nobackup`` because it can always be downloaded again.

    uv run .agents/skills/build-idea-library/scripts/transcribe.py [--sources DIR] [--model base.en]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from library_sources import AUDIO_SUFFIXES, TRANSCRIPT_SUFFIX  # noqa: E402
from library_store import DEFAULT_SOURCES_DIR, REPO_ROOT, write_json  # noqa: E402

MODEL_CACHE = REPO_ROOT / "data/.state/models/faster-whisper"


def ensure_rebuildable_store_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".nobackup").touch()
    return path


def pending_audio(sources: Path) -> list[Path]:
    return [
        p
        for p in sorted(sources.rglob("*"))
        if p.is_file()
        and p.suffix.lower() in AUDIO_SUFFIXES
        and not p.with_name(p.name + TRANSCRIPT_SUFFIX).exists()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES_DIR)
    parser.add_argument(
        "--model",
        default="base.en",
        help="a faster-whisper model name (base.en, small, medium, ...)",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="the spoken language, e.g. en (default: detect)",
    )
    args = parser.parse_args()
    todo = pending_audio(args.sources)
    if not todo:
        print("No audio waiting for a transcript.")
        return 0
    # Only installed in this script's own environment (its PEP 723 header).
    from faster_whisper import WhisperModel  # ty: ignore[unresolved-import]

    cache = ensure_rebuildable_store_dir(MODEL_CACHE)
    print(f"Transcribing {len(todo)} recording(s) with {args.model}.", flush=True)
    model = WhisperModel(
        args.model, device="cpu", compute_type="int8", download_root=str(cache)
    )
    failures = 0
    for path in todo:
        began = time.monotonic()
        try:
            segments, info = model.transcribe(
                str(path),
                language=args.language,
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            captured = [
                {
                    "start": s.start,
                    "end": s.end,
                    "text": s.text.strip(),
                    "avg_logprob": s.avg_logprob,
                    "no_speech_prob": s.no_speech_prob,
                }
                for s in segments
                if s.text.strip()
            ]
        except Exception as exc:  # one unreadable file must not stop the rest
            failures += 1
            print(
                f"Could not transcribe {path.name}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        write_json(
            path.with_name(path.name + TRANSCRIPT_SUFFIX),
            {
                "platform": "whisper",
                "audio": path.name,
                "model": f"faster-whisper/{args.model}",
                "language": getattr(info, "language", args.language),
                "duration_s": round(getattr(info, "duration", 0.0), 2),
                "status": "automatic transcription",
                "segments": captured,
                "text": " ".join(s["text"] for s in captured),
            },
        )
        print(
            f"{path.name}: {len(captured)} segments in {time.monotonic() - began:.0f}s",
            flush=True,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
