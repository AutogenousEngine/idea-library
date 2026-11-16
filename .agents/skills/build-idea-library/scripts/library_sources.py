"""Read a folder of source material into source records.

A source record is a plain dict:

- ``id``, ``title``, ``kind``, ``url`` (the origin link, or None), ``date``
- ``path``: the file's path relative to the sources folder, ``sha256`` of its bytes
- ``text``: the whole source as text (every speaker, for a transcript)
- ``raw``: the author's own words -- what ideas are pulled from and quotes are checked
  against. For a speaker-labelled transcript it holds only the author's turns, each
  headed ``[m:ss] Speaker``; otherwise it equals ``text``.
- ``segments``: for a transcript, ``[{start_ms, speaker, text}]``; else None
- ``turns``: for a speaker-labelled transcript, ``[{start, end, start_ms}]`` -- where each
  author turn's text sits in ``raw`` -- so a quote can be given its timestamp
- ``audio``: the audio file's path relative to the sources folder, for a voice note
- ``transcription_note``: shown beside quotes of an automatic transcript
- ``status``: ``ok``, or why the source cannot feed idea finding (``skip_reason``)

Readers are tolerant: every transcript shape the fetchers write, Plaud's and Otter's
own exports, whisper output, captions and plain ``[m:ss] Name: text`` lines all land
in the same record.
"""

from __future__ import annotations

import fnmatch
import hashlib
import html
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
HTML_SUFFIXES = {".html", ".htm"}
CAPTION_SUFFIXES = {".srt", ".vtt"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".webm"}
SIDECAR_SUFFIX = ".source.json"
TRANSCRIPT_SUFFIX = ".transcript.json"
READABLE_SUFFIXES = TEXT_SUFFIXES | HTML_SUFFIXES | CAPTION_SUFFIXES | {".pdf", ".json"}

AUTOMATIC_TRANSCRIPT_NOTE = (
    "Automatic transcript; confirm exact wording against the recording."
)
# Metadata a sidecar file or front matter may set on a source.
METADATA_KEYS = ("title", "url", "kind", "date")


class SourceReadError(ValueError):
    """A file that looked readable but whose contents could not be understood."""


def slugify(text: str, *, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "source"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def format_timestamp(ms: int | float | None) -> str:
    seconds = int((ms or 0) // 1000)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def parse_clock(value: str) -> int:
    """``1:02:03``, ``02:03`` or ``02:03,500`` / ``02:03.500`` as milliseconds."""
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return int(round(seconds * 1000))


# ---- discovery ---------------------------------------------------------------


def is_excluded(relative: str, patterns: list[str]) -> bool:
    name = relative.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(relative, p) or fnmatch.fnmatch(name, p) for p in patterns
    )


def discover(
    root: Path, excludes: list[str]
) -> tuple[list[Path], list[dict[str, str]]]:
    """The files under ``root`` to read, and the ones set aside with a reason."""
    readable: list[Path] = []
    skipped: list[dict[str, str]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if relative.endswith(SIDECAR_SUFFIX):
            continue
        if is_excluded(relative, excludes):
            skipped.append({"path": relative, "reason": "excluded"})
            continue
        suffix = path.suffix.lower()
        if suffix in AUDIO_SUFFIXES:
            if not path.with_name(path.name + TRANSCRIPT_SUFFIX).exists():
                skipped.append(
                    {
                        "path": relative,
                        "reason": "audio with no transcript yet (run transcribe)",
                    }
                )
            continue
        if suffix not in READABLE_SUFFIXES:
            skipped.append(
                {
                    "path": relative,
                    "reason": f"unsupported file type {suffix or '(none)'}",
                }
            )
            continue
        readable.append(path)
    return readable, skipped


# ---- text helpers ------------------------------------------------------------

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() in METADATA_KEYS:
            meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, text[match.end() :]


def title_from_text(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"#{1,6}\s+(.*)", stripped)
        if heading:
            return heading.group(1).strip()
        return stripped if len(stripped) <= 120 else fallback
    return fallback


class _TextExtractor(HTMLParser):
    BLOCKS = {
        "p",
        "div",
        "br",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "tr",
        "section",
        "article",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.h1 = ""
        self.canonical = ""
        self._skip = 0
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        elif tag == "link" and values.get("rel") == "canonical":
            self.canonical = values.get("href", "")
        elif (
            tag == "meta" and values.get("property") == "og:url" and not self.canonical
        ):
            self.canonical = values.get("content", "")
        if tag in self.BLOCKS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        if tag in self.BLOCKS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        if self._in_h1:
            self.h1 += data
        self.parts.append(data)


def html_to_text(markup: str) -> tuple[str, str, str]:
    """(text, title, canonical url) of an HTML document or fragment."""
    parser = _TextExtractor()
    parser.feed(markup)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, (parser.h1 or parser.title).strip(), parser.canonical.strip()


# Transcript lines: "[01:02] Name: text", "01:02 Name: text", "[01:02] text".
_BRACKET_LINE = re.compile(
    r"^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s+(?:([^:\n]{1,60}?):\s+)?(.+)$"
)
# Otter's text export: a "Name  0:05" header line, then the words on the next lines.
_OTTER_HEADER = re.compile(r"^(\S.{0,60}?)\s{2,}(\d{1,2}:\d{2}(?::\d{2})?)\s*$")


def parse_transcript_lines(text: str) -> list[dict[str, Any]] | None:
    """Segments of a plain-text transcript, or None when the text is not one."""
    lines = [line.rstrip() for line in text.splitlines()]
    nonblank = [line for line in lines if line.strip()]
    if not nonblank:
        return None
    headers = [i for i, line in enumerate(lines) if _OTTER_HEADER.match(line)]
    if len(headers) >= 2 and len(headers) * 3 >= len(nonblank):
        segments = []
        for n, start in enumerate(headers):
            match = _OTTER_HEADER.match(lines[start])
            assert match is not None
            end = headers[n + 1] if n + 1 < len(headers) else len(lines)
            body = " ".join(
                line.strip() for line in lines[start + 1 : end] if line.strip()
            )
            if body:
                segments.append(
                    {
                        "start_ms": parse_clock(match.group(2)),
                        "speaker": match.group(1).strip(),
                        "text": body,
                    }
                )
        return segments or None
    matches = [_BRACKET_LINE.match(line.strip()) for line in nonblank]
    hits = [m for m in matches if m]
    if len(hits) < 2 or len(hits) * 4 < len(nonblank) * 3:
        return None
    segments = []
    for match in hits:
        segments.append(
            {
                "start_ms": parse_clock(match.group(1)),
                "speaker": (match.group(2) or "").strip(),
                "text": match.group(3).strip(),
            }
        )
    return segments


def parse_captions(text: str) -> list[dict[str, Any]]:
    """Segments of an .srt or .vtt caption file (a ``<v Name>`` voice tag names the speaker)."""
    segments: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    for block in blocks:
        lines = [line for line in block.strip().splitlines() if line.strip()]
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        start = lines[timing].split("-->")[0].strip()
        body = " ".join(lines[timing + 1 :]).strip()
        speaker = ""
        voice = re.match(r"<v(?:\.[^ >]*)?\s+([^>]+)>", body)
        if voice:
            speaker = voice.group(1).strip()
        body = re.sub(r"<[^>]+>", "", body).strip()
        if body:
            segments.append(
                {
                    "start_ms": parse_clock(start),
                    "speaker": speaker,
                    "text": html.unescape(body),
                }
            )
    return segments


def _segment_text(segment: dict[str, Any]) -> str:
    return str(
        segment.get("content") or segment.get("text") or segment.get("transcript") or ""
    ).strip()


def normalize_segments(
    items: list[Any], *, seconds: bool | None = None
) -> list[dict[str, Any]]:
    """Segments from any of the platform shapes: Plaud (``start_time`` ms, ``content``),
    whisper (``start`` seconds, ``text``), Otter (``start_offset``/``start`` ms or a clock
    string, ``speaker``/``speaker_name``)."""
    segments: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _segment_text(item)
        if not text:
            continue
        start: Any = item.get(
            "start_ms",
            item.get("start_time", item.get("start_offset", item.get("start", 0))),
        )
        if isinstance(start, str):
            start_ms = (
                parse_clock(start) if ":" in start else int(float(start or 0) * 1000)
            )
        elif seconds is True or (
            seconds is None
            and "start" in item
            and "start_time" not in item
            and isinstance(start, float)
        ):
            start_ms = int(round(float(start) * 1000))
        else:
            start_ms = int(start or 0)
        speaker = (
            item.get("speaker")
            or item.get("speaker_name")
            or item.get("original_speaker")
            or ""
        )
        if isinstance(speaker, dict):
            speaker = speaker.get("name") or speaker.get("speaker_name") or ""
        segments.append(
            {"start_ms": start_ms, "speaker": str(speaker).strip(), "text": text}
        )
    return segments


def segments_to_text(segments: list[dict[str, Any]]) -> str:
    lines = []
    for segment in segments:
        speaker = f" {segment['speaker']}:" if segment.get("speaker") else ""
        lines.append(
            f"[{format_timestamp(segment['start_ms'])}]{speaker} {segment['text']}"
        )
    return "\n\n".join(lines)


# ---- readers -----------------------------------------------------------------


def _record(path: Path, root: Path, **fields: Any) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    record: dict[str, Any] = {
        "id": "",
        "title": path.stem.replace("-", " ").replace("_", " ").strip() or path.name,
        "kind": "Writing",
        "url": None,
        "date": None,
        "path": relative,
        "sha256": file_sha256(path),
        "text": "",
        "segments": None,
        "audio": None,
        "transcription_note": None,
        "platform": None,
    }
    record.update({k: v for k, v in fields.items() if v is not None})
    return record


def read_json_source(path: Path, root: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SourceReadError(f"not valid JSON ({exc.msg})") from exc
    if isinstance(data, list):
        segments = normalize_segments(data)
        if not segments:
            raise SourceReadError("a JSON list with no transcript segments")
        return _record(
            path,
            root,
            kind="Transcript",
            segments=segments,
            transcription_note=AUTOMATIC_TRANSCRIPT_NOTE,
        )
    if not isinstance(data, dict):
        raise SourceReadError("JSON that is neither an object nor a list")
    platform = str(data.get("platform") or "").lower()
    if "body_html" in data or platform == "substack":
        body = data.get("body_html") or ""
        text, _, _ = html_to_text(body)
        if data.get("subtitle"):
            text = f"{data['subtitle']}\n\n{text}"
        return _record(
            path,
            root,
            platform="substack",
            title=data.get("title"),
            kind="Substack article",
            url=data.get("canonical_url") or data.get("url"),
            date=data.get("post_date") or data.get("date"),
            text=text,
        )
    raw_segments = data.get("segments")
    if raw_segments is None and isinstance(data.get("transcript"), list):
        raw_segments = data["transcript"]
    if isinstance(raw_segments, list):
        is_whisper = platform == "whisper" or (
            "language" in data
            or "model" in data
            or any(isinstance(s, dict) and "avg_logprob" in s for s in raw_segments)
        )
        segments = normalize_segments(
            raw_segments, seconds=True if is_whisper else None
        )
        defaults = {
            "plaud": "Plaud recording",
            "otter": "Otter conversation",
            "whisper": "Voice note",
        }
        kind = defaults.get(platform, "Voice note" if is_whisper else "Transcript")
        audio = data.get("audio")
        if audio:
            audio_path = (path.parent / audio).resolve()
            audio = (
                audio_path.relative_to(root.resolve()).as_posix()
                if audio_path.exists()
                else None
            )
        title = data.get("title") or data.get("name")
        if not title and audio:
            title = Path(audio).stem.replace("-", " ").replace("_", " ")
        return _record(
            path,
            root,
            platform=platform or ("whisper" if is_whisper else None),
            title=title,
            kind=kind,
            url=data.get("url"),
            date=data.get("date") or data.get("start_at"),
            segments=segments,
            audio=audio,
            transcription_note=AUTOMATIC_TRANSCRIPT_NOTE,
        )
    for key in ("text", "transcript_text", "transcript", "body"):
        if isinstance(data.get(key), str) and data[key].strip():
            text = data[key]
            segments = parse_transcript_lines(text)
            return _record(
                path,
                root,
                platform=platform or None,
                title=data.get("title"),
                kind=data.get("kind")
                or (
                    "Otter conversation"
                    if platform == "otter"
                    else "Transcript"
                    if segments
                    else "Writing"
                ),
                url=data.get("url"),
                date=data.get("date"),
                text=text if not segments else "",
                segments=segments,
                transcription_note=AUTOMATIC_TRANSCRIPT_NOTE if segments else None,
            )
    raise SourceReadError(
        "JSON with no text, transcript or post body this reader knows"
    )


def read_text_source(path: Path, root: Path) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8", errors="replace")
    meta, body = split_front_matter(content)
    segments = parse_transcript_lines(body)
    if segments:
        return _record(
            path,
            root,
            title=meta.get("title"),
            kind=meta.get("kind") or "Transcript",
            url=meta.get("url"),
            date=meta.get("date"),
            segments=segments,
            transcription_note=AUTOMATIC_TRANSCRIPT_NOTE,
        )
    title = meta.get("title") or title_from_text(body, path.stem.replace("-", " "))
    return _record(
        path,
        root,
        title=title,
        kind=meta.get("kind"),
        url=meta.get("url"),
        date=meta.get("date"),
        text=body.strip(),
    )


def read_html_source(path: Path, root: Path) -> dict[str, Any]:
    text, title, canonical = html_to_text(
        path.read_text(encoding="utf-8", errors="replace")
    )
    return _record(path, root, title=title or None, url=canonical or None, text=text)


def read_pdf_source(path: Path, root: Path) -> dict[str, Any]:
    if shutil.which("pdftotext") is None:
        raise SourceReadError(
            "reading PDFs needs pdftotext (the poppler-utils package)"
        )
    result = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", str(path), "-"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise SourceReadError(
            f"pdftotext could not read it: {result.stderr.strip()[:200]}"
        )
    text = re.sub(r"[ \t]+\n", "\n", result.stdout).replace("\f", "\n\n").strip()
    if len(text.split()) < 5:
        raise SourceReadError("a PDF with no text layer (scanned pages need OCR first)")
    return _record(path, root, title=title_from_text(text, path.stem), text=text)


def read_caption_source(path: Path, root: Path) -> dict[str, Any]:
    segments = parse_captions(path.read_text(encoding="utf-8", errors="replace"))
    if not segments:
        raise SourceReadError("a caption file with no cues")
    return _record(
        path,
        root,
        kind="Video captions",
        segments=segments,
        transcription_note=AUTOMATIC_TRANSCRIPT_NOTE,
    )


def read_file(path: Path, root: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        record = read_json_source(path, root)
    elif suffix in TEXT_SUFFIXES:
        record = read_text_source(path, root)
    elif suffix in HTML_SUFFIXES:
        record = read_html_source(path, root)
    elif suffix == ".pdf":
        record = read_pdf_source(path, root)
    elif suffix in CAPTION_SUFFIXES:
        record = read_caption_source(path, root)
    else:
        raise SourceReadError(f"unsupported file type {suffix}")
    sidecar = path.with_name(path.name + SIDECAR_SUFFIX)
    if not sidecar.exists():
        sidecar = path.with_name(path.stem + SIDECAR_SUFFIX)
    if sidecar.exists() and sidecar != path:
        try:
            extra = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SourceReadError(
                f"its sidecar {sidecar.name} is not valid JSON"
            ) from exc
        record.update({k: extra[k] for k in METADATA_KEYS if extra.get(k)})
    if record["segments"] is not None:
        record["text"] = segments_to_text(record["segments"])
    return record


# ---- attribution -------------------------------------------------------------


def _speaker_matches(speaker: str, authors: list[str]) -> bool:
    name = re.sub(r"\s*\(you\)\s*$", "", speaker.strip(), flags=re.I).casefold()
    return any(name == a.strip().casefold() for a in authors if a.strip())


def attribute(record: dict[str, Any], authors: list[str]) -> dict[str, Any]:
    """Fill ``raw``, ``turns``, ``status`` and ``skip_reason`` from who is speaking."""
    record["turns"] = None
    record["status"] = "ok"
    record["skip_reason"] = None
    segments = record.get("segments")
    if segments is None:
        record["raw"] = record["text"]
    else:
        speakers = sorted({s["speaker"] for s in segments if s["speaker"]})
        if not speakers:
            # No names (a voice note, captions): every word is the author's.
            chosen, labelled = segments, False
        elif authors:
            chosen, labelled = (
                [s for s in segments if _speaker_matches(s["speaker"], authors)],
                True,
            )
        elif len(speakers) == 1:
            chosen, labelled = segments, True
        else:
            chosen, labelled = [], True
        parts: list[str] = []
        turns: list[dict[str, int]] = []
        position = 0
        for segment in chosen:
            header = (
                f"[{format_timestamp(segment['start_ms'])}] {segment['speaker']}\n"
                if labelled
                else ""
            )
            if parts:
                position += 2
            start = position + len(header)
            parts.append(header + segment["text"])
            position = start + len(segment["text"])
            turns.append(
                {"start": start, "end": position, "start_ms": segment["start_ms"]}
            )
        record["raw"] = "\n\n".join(parts)
        record["turns"] = turns
        if labelled and not authors and len(speakers) > 1:
            record["status"] = "skipped"
            record["skip_reason"] = (
                "several speakers and no author named (pass --author with the author's speaker name)"
            )
        elif labelled and not turns:
            record["status"] = "skipped"
            record["skip_reason"] = (
                "the author never speaks in it (no turn labelled "
                + " / ".join(authors)
                + ")"
            )
        elif labelled:
            names = " / ".join(sorted({s["speaker"] for s in chosen}))
            record["transcription_note"] = (
                f"Automatic transcript. Passages are the turns labelled as {names}; "
                "speaker labels and wording may need correction."
            )
    if record["status"] == "ok" and len(record["raw"].split()) < 5:
        record["status"] = "skipped"
        record["skip_reason"] = "no words to read (empty, or no speech)"
    return record


def assign_ids(
    records: list[dict[str, Any]],
    taken: dict[str, str],
    reserved: set[str] | None = None,
) -> None:
    """Give each record a stable id from its path; ``taken`` maps path -> id from earlier runs,
    and ``reserved`` holds ids a new source must not take (sources already in the library)."""
    used = set(taken.values()) | (reserved or set())
    for record in records:
        if record["path"] in taken:
            record["id"] = taken[record["path"]]
            continue
        stem = record["path"]
        if stem.endswith(TRANSCRIPT_SUFFIX):
            stem = stem[: -len(TRANSCRIPT_SUFFIX)]
        base = slugify(stem.rsplit(".", 1)[0])
        candidate = base
        counter = 2
        while candidate in used:
            candidate = f"{base}-{counter}"
            counter += 1
        record["id"] = candidate
        used.add(candidate)
        taken[record["path"]] = candidate
