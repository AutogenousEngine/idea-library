"""The model-judgement steps: find ideas in a source, sort ideas into chapters, connect them.

Every step takes a ``complete(prompt, system) -> (text, cost_usd)`` callable, so the real
model and a scripted fake in tests run the same code. Every quote a model returns is
checked against the stored source text (``locate_quote``) before it is kept: a quote is
evidence only if it appears in the source word for word.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from library_sources import format_timestamp

Complete = Callable[[str, str], tuple[str, float]]

DEFAULT_MODEL = "claude-sonnet-5-5"
# Part of every cached extraction's key: change it when the extraction prompt changes
# in a way that should re-read sources already processed.
PROMPT_VERSION = "1"
FORMATS = [
    "Twitter post",
    "Substack note",
    "Substack article",
    "Short video",
    "Long video",
]
MAX_IDEAS_PER_CHUNK = 8
CHUNK_WORDS = 6000
MIN_QUOTE_CHARS = 12
CROSS_BATCH = 25

# ---- quote checking ----------------------------------------------------------

_EQUIVALENT = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "′": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "″": '"',
    "–": "-",
    "—": "-",
    "−": "-",
    " ": " ",
}
_HEADER = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")


def _normalized_with_map(text: str) -> tuple[str, list[int]]:
    """``text`` with typographic variants folded and whitespace runs collapsed, plus the
    index in ``text`` of every character kept."""
    out: list[str] = []
    index: list[int] = []
    previous_space = False
    for i, char in enumerate(text):
        if char == "…":
            for dot in "...":
                out.append(dot)
                index.append(i)
            previous_space = False
            continue
        char = _EQUIVALENT.get(char, char)
        if char.isspace():
            if previous_space:
                continue
            char = " "
            previous_space = True
        else:
            previous_space = False
        out.append(char)
        index.append(i)
    return "".join(out), index


def locate_quote(quote: str, raw: str) -> str | None:
    """The exact text of ``raw`` that ``quote`` copies, or None.

    Exact matches pass as they are. A quote differing only in curly/straight quotes,
    dashes, ellipses or spacing is replaced by the source's own wording. Anything else
    -- a paraphrase, a quote spanning a transcript turn header -- is not evidence."""
    if not isinstance(quote, str):
        return None
    quote = quote.strip()
    if len(quote) > 1 and quote[0] in '"“' and quote[-1] in '"”':
        quote = quote[1:-1].strip()
    quote = re.sub(r"^(\.\.\.|…)\s*", "", quote)
    quote = re.sub(r"\s*(\.\.\.|…)$", "", quote).strip()
    if len(quote) < MIN_QUOTE_CHARS or _HEADER.search(quote):
        return None
    if quote in raw:
        return quote
    norm_raw, index = _normalized_with_map(raw)
    norm_quote, _ = _normalized_with_map(quote)
    norm_quote = norm_quote.strip()
    position = norm_raw.find(norm_quote)
    if position < 0:
        return None
    found = raw[index[position] : index[position + len(norm_quote) - 1] + 1]
    return None if _HEADER.search(found) else found


def evidence_for(source: dict[str, Any], quote: str) -> dict[str, Any] | None:
    """An evidence entry for ``quote`` in ``source``, timestamped for a transcript."""
    found = locate_quote(quote, source["raw"])
    if found is None:
        return None
    entry: dict[str, Any] = {"source_id": source["id"], "quote": found}
    turns = source.get("turns")
    if turns:
        at = source["raw"].find(found)
        for turn in turns:
            if turn["start"] <= at < turn["end"] or (
                at < turn["start"] and turn is turns[0]
            ):
                entry["start_ms"] = turn["start_ms"]
                entry["timestamp"] = format_timestamp(turn["start_ms"])
                break
    return entry


# ---- model replies ------------------------------------------------------------


def parse_json_reply(text: str) -> dict[str, Any]:
    """The JSON object in a model reply, tolerating code fences and stray prose."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("the model's reply held no JSON object") from None
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("the model's reply was JSON but not an object")
    return value


def _clean_str(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


# ---- extract -----------------------------------------------------------------

EXTRACT_SYSTEM = (
    "You find the distinct ideas an author expresses in their own material, for a personal idea "
    "library. You never invent claims, and every quote you give is copied character for character "
    "from the source. Reply with one JSON object only."
)


def extract_prompt(source: dict[str, Any], chunk: str, part: str) -> str:
    return f"""Below is {part}one source by the author: "{source["title"]}" ({source["kind"]}).
{"It holds only the author's own turns from a recorded conversation, each headed [time] Speaker." if source.get("turns") and "] " in chunk[:40] else ""}
Pull out the distinct ideas the author actually expresses: 0 to {MAX_IDEAS_PER_CHUNK}. Fewer is better than padding;
small talk, logistics and other people's ideas are not the author's ideas. For each idea give:
- "title": 2 to 6 plain words
- "summary": one or two sentences describing the idea as the author holds it. Call the author "the author" or "they", never "he" or "she". Add nothing the source does not say.
- "quotes": 1 to 3 passages copied EXACTLY from the source, a sentence or two each, where the author expresses this idea. Never include the [time] Speaker headers.
- "writing_seed": one sentence proposing a piece of content the author could make from the idea, or null
- "formats": the formats from {json.dumps(FORMATS)} that would suit it
Return {{"ideas": [...]}}.

SOURCE:
<<<
{chunk}
>>>"""


def chunk_text(raw: str, limit: int = CHUNK_WORDS) -> list[str]:
    """``raw`` split at paragraph breaks into pieces of at most about ``limit`` words."""
    if len(raw.split()) <= limit:
        return [raw]
    chunks: list[str] = []
    current: list[str] = []
    words = 0
    for paragraph in raw.split("\n\n"):
        count = len(paragraph.split())
        if current and words + count > limit:
            chunks.append("\n\n".join(current))
            current, words = [], 0
        current.append(paragraph)
        words += count
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def extract_source(source: dict[str, Any], complete: Complete) -> dict[str, Any]:
    """The ideas in one source, each with quotes that passed the word-for-word check."""
    chunks = chunk_text(source["raw"])
    ideas: list[dict[str, Any]] = []
    dropped_quotes: list[str] = []
    dropped_ideas: list[str] = []
    cost = 0.0
    for n, chunk in enumerate(chunks):
        part = f"part {n + 1} of {len(chunks)} of " if len(chunks) > 1 else ""
        text, spent = complete(extract_prompt(source, chunk, part), EXTRACT_SYSTEM)
        cost += spent
        for item in parse_json_reply(text).get("ideas") or []:
            if not isinstance(item, dict):
                continue
            title = _clean_str(item.get("title"), 80)
            summary = _clean_str(item.get("summary"), 600)
            if not title or not summary:
                continue
            evidence: list[dict[str, Any]] = []
            for quote in (item.get("quotes") or [])[:3]:
                entry = evidence_for(source, quote)
                if entry is None:
                    dropped_quotes.append(str(quote)[:300])
                elif all(e["quote"] != entry["quote"] for e in evidence):
                    evidence.append(entry)
            if not evidence:
                dropped_ideas.append(title)
                continue
            seed = item.get("writing_seed")
            ideas.append(
                {
                    "title": title,
                    "summary": summary,
                    "writing_seed": _clean_str(seed, 400)
                    if isinstance(seed, str) and seed.strip()
                    else None,
                    "formats": [f for f in item.get("formats") or [] if f in FORMATS],
                    "evidence": evidence,
                }
            )
    return {
        "source_id": source["id"],
        "sha256": source["sha256"],
        "prompt_version": PROMPT_VERSION,
        "ideas": ideas,
        "dropped_quotes": dropped_quotes,
        "dropped_ideas": dropped_ideas,
        "cost_usd": round(cost, 6),
    }


# ---- organize ----------------------------------------------------------------

ORGANIZE_SYSTEM = (
    "You organise a person's ideas into the chapters of their personal idea library. Chapter names "
    "come from the material itself. Reply with one JSON object only."
)
FALLBACK_CHAPTER = "More ideas"


def organize_prompt(
    chapters: list[dict[str, str]],
    existing: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    return f"""The library's chapters so far (keep every one, with its exact name):
{json.dumps(chapters, ensure_ascii=False)}

Ideas already in the library (id, title, summary, chapter):
{json.dumps(existing, ensure_ascii=False)}

New ideas to place (cid, title, summary):
{json.dumps(candidates, ensure_ascii=False)}

For every new idea, either put it in a chapter or merge it into an idea that says the same thing
(an existing id, or another new cid placed earlier in your list). Merge only true duplicates.
You may add new chapters when no existing one fits. Aim for 4 to 9 chapters in all, each a short
name of 1 to 4 words drawn from the ideas themselves, with a subtitle of under 10 words saying what
the chapter is about. Order chapters as a reader should meet them.
Also name the whole body of work: "collection", 2 to 5 words drawn from what the ideas are about
(the way a book or a newsletter is named, not a description of the library), and "reading_intro",
one sentence of under 16 words inviting a reader through the chapters in order.
Return {{"collection": ..., "reading_intro": ..., "chapters": [{{"name": ..., "subtitle": ...}}], "placements": [{{"cid": ..., "chapter": ...}} or {{"cid": ..., "merge_into": ...}}]}}."""


def organize(
    chapters: list[dict[str, str]],
    existing: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    complete: Complete,
) -> dict[str, Any]:
    """Chapters for the whole library, a placement for every candidate, and a name and
    reading-page intro for the whole collection.

    ``existing`` ideas keep their chapter. ``candidates`` carry a ``cid``. A placement the
    model leaves out or gets wrong falls back to a chapter of its own, never lost."""
    if not candidates:
        return {"chapters": chapters, "placements": [], "cost_usd": 0.0}
    text, cost = complete(
        organize_prompt(
            chapters,
            [
                {
                    "id": n["id"],
                    "title": n["title"],
                    "summary": n["summary"],
                    "chapter": n["group"],
                }
                for n in existing
            ],
            [
                {"cid": c["cid"], "title": c["title"], "summary": c["summary"]}
                for c in candidates
            ],
        ),
        ORGANIZE_SYSTEM,
    )
    reply = parse_json_reply(text)
    names = [c["name"] for c in chapters]
    result_chapters = [dict(c) for c in chapters]
    for item in reply.get("chapters") or []:
        if not isinstance(item, dict):
            continue
        name = _clean_str(item.get("name"), 60)
        if name and name not in names:
            names.append(name)
            result_chapters.append(
                {"name": name, "subtitle": _clean_str(item.get("subtitle"), 120)}
            )
        elif name in names:
            for chapter in result_chapters:
                if chapter["name"] == name and not chapter.get("subtitle"):
                    chapter["subtitle"] = _clean_str(item.get("subtitle"), 120)
    existing_ids = {n["id"] for n in existing}
    placed: dict[str, dict[str, str]] = {}
    order = [c["cid"] for c in candidates]
    for item in reply.get("placements") or []:
        if (
            not isinstance(item, dict)
            or item.get("cid") not in order
            or item["cid"] in placed
        ):
            continue
        cid = item["cid"]
        target = item.get("merge_into")
        if target and (
            target in existing_ids
            or (target in placed and target != cid and "chapter" in placed[target])
        ):
            placed[cid] = {"cid": cid, "merge_into": target}
        elif item.get("chapter") in names:
            placed[cid] = {"cid": cid, "chapter": item["chapter"]}
    placements = []
    for cid in order:
        if cid not in placed:
            if FALLBACK_CHAPTER not in names:
                names.append(FALLBACK_CHAPTER)
                result_chapters.append(
                    {
                        "name": FALLBACK_CHAPTER,
                        "subtitle": "ideas not yet sorted into a chapter",
                    }
                )
            placed[cid] = {"cid": cid, "chapter": FALLBACK_CHAPTER}
        placements.append(placed[cid])
    used = {p["chapter"] for p in placements if "chapter" in p} | {
        n["group"] for n in existing
    }
    result_chapters = [c for c in result_chapters if c["name"] in used]
    return {
        "chapters": result_chapters,
        "placements": placements,
        "collection": _clean_str(reply.get("collection"), 80) or None,
        "reading_intro": _clean_str(reply.get("reading_intro"), 200) or None,
        "cost_usd": round(cost, 6),
    }


# ---- connect -----------------------------------------------------------------

CONNECT_SYSTEM = (
    "You map how a person's ideas relate, for their personal idea library. You keep links the "
    "author stated apart from links you are only suggesting. Reply with one JSON object only."
)

_CONNECTION_RULES = """Each connection:
- "source", "target": idea ids; it reads "source <label> target"
- "label": a short verb phrase, such as "leads to", "may help explain", "is distinguished from"
- "basis": "expressed" only when the author's own words link the two ideas; otherwise "suggested"
- "why": one sentence; for a suggested link, say plainly that it is an interpretation
- "quotes": for an expressed link, 1 or 2 passages copied EXACTLY from the author's words that show
  the link, each as {"source_id": ..., "quote": ...}; may be empty for a suggested link
Skip weak or merely topical links. At most 3 connections per new idea."""


def within_source_prompt(source: dict[str, Any], ideas: list[dict[str, Any]]) -> str:
    cards = [
        {"id": i["id"], "title": i["title"], "summary": i["summary"]} for i in ideas
    ]
    return f"""These ideas all come from one source by the author, "{source["title"]}" (source_id "{source["id"]}"):
{json.dumps(cards, ensure_ascii=False)}

Propose the meaningful connections between them.
{_CONNECTION_RULES}
Return {{"connections": [...]}}.

SOURCE:
<<<
{source["raw"]}
>>>"""


def cross_prompt(
    new_cards: list[dict[str, Any]], all_cards: list[dict[str, Any]]
) -> str:
    return f"""Ideas across the author's whole library (id, title, summary, chapter, and the author's quotes with their source_id):
{json.dumps(all_cards, ensure_ascii=False)}

Propose meaningful connections between each of these new ideas and any other idea in the library:
{json.dumps([c["id"] for c in new_cards])}
{_CONNECTION_RULES}
For an expressed link, the quotes must be copied from the quotes shown above.
Return {{"connections": [...]}}."""


def idea_card(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node["id"],
        "title": node["title"],
        "summary": node["summary"],
        "chapter": node.get("group", ""),
        "quotes": [
            {"source_id": e["source_id"], "quote": e["quote"]}
            for e in node.get("evidence", [])[:2]
        ],
    }


def check_connections(
    reply: dict[str, Any],
    node_ids: set[str],
    sources: dict[str, dict[str, Any]],
    seen_pairs: set[frozenset[str]],
    default_source: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The connections in ``reply`` that hold up: real ideas, a new pair, and evidence
    that passes the word-for-word check. An "expressed" link with no surviving quote
    becomes "suggested"."""
    kept: list[dict[str, Any]] = []
    stats = {"rejected": 0, "downgraded": 0, "dropped_quotes": 0}
    for item in reply.get("connections") or []:
        if not isinstance(item, dict):
            continue
        a, b = item.get("source"), item.get("target")
        pair = frozenset((a, b)) if isinstance(a, str) and isinstance(b, str) else None
        label = _clean_str(item.get("label"), 60)
        why = _clean_str(item.get("why"), 400)
        if (
            a not in node_ids
            or b not in node_ids
            or a == b
            or not label
            or not why
            or pair is None
            or pair in seen_pairs
        ):
            stats["rejected"] += 1
            continue
        evidence: list[dict[str, Any]] = []
        for quote in (item.get("quotes") or [])[:2]:
            source_id = (
                quote.get("source_id") if isinstance(quote, dict) else default_source
            )
            text = quote.get("quote") if isinstance(quote, dict) else quote
            source = sources.get(source_id or default_source or "")
            entry = (
                evidence_for(source, text) if source and isinstance(text, str) else None
            )
            if entry is None:
                stats["dropped_quotes"] += 1
            elif all(e["quote"] != entry["quote"] for e in evidence):
                evidence.append(entry)
        basis = (
            item.get("basis")
            if item.get("basis") in ("expressed", "suggested")
            else "suggested"
        )
        if basis == "expressed" and not evidence:
            basis = "suggested"
            stats["downgraded"] += 1
        seen_pairs.add(pair)
        kept.append(
            {
                "source": a,
                "target": b,
                "label": label,
                "basis": basis,
                "why": why,
                "evidence": evidence,
            }
        )
    return kept, stats


def connect(
    new_nodes: list[dict[str, Any]],
    all_nodes: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    existing_edges: list[dict[str, Any]],
    complete: Complete,
    *,
    map_parallel: Callable[[Callable[[Any], Any], list[Any]], list[Any]] | None = None,
) -> dict[str, Any]:
    """Connections for ``new_nodes``: first within each source (with its text, so the passage
    where the author links two ideas can be quoted), then across the whole library."""
    run = map_parallel or (lambda fn, items: [fn(i) for i in items])
    node_ids = {n["id"] for n in all_nodes}
    seen = {frozenset((e["source"], e["target"])) for e in existing_edges}
    new_ids = {n["id"] for n in new_nodes}
    by_source: dict[str, list[dict[str, Any]]] = {}
    for node in all_nodes:
        home = node["evidence"][0]["source_id"] if node.get("evidence") else None
        if home in sources:
            by_source.setdefault(home, []).append(node)
    groups = [
        (sid, ideas)
        for sid, ideas in by_source.items()
        if len(ideas) > 1 and any(i["id"] in new_ids for i in ideas)
    ]
    totals = {"rejected": 0, "downgraded": 0, "dropped_quotes": 0}
    edges: list[dict[str, Any]] = []
    cost = 0.0

    def ask_within(
        group: tuple[str, list[dict[str, Any]]],
    ) -> tuple[dict[str, Any], float, str]:
        sid, ideas = group
        text, spent = complete(
            within_source_prompt(sources[sid], ideas), CONNECT_SYSTEM
        )
        return parse_json_reply(text), spent, sid

    for reply, spent, sid in run(ask_within, groups):
        cost += spent
        kept, stats = check_connections(
            reply, node_ids, sources, seen, default_source=sid
        )
        edges.extend(kept)
        for key in totals:
            totals[key] += stats[key]
    if len(all_nodes) > 1:
        cards = [idea_card(n) for n in all_nodes]
        batches = [
            new_nodes[i : i + CROSS_BATCH]
            for i in range(0, len(new_nodes), CROSS_BATCH)
        ]

        def ask_cross(batch: list[dict[str, Any]]) -> tuple[dict[str, Any], float]:
            text, spent = complete(cross_prompt(batch, cards), CONNECT_SYSTEM)
            return parse_json_reply(text), spent

        for reply, spent in run(ask_cross, batches):
            cost += spent
            kept, stats = check_connections(reply, node_ids, sources, seen)
            edges.extend(kept)
            for key in totals:
                totals[key] += stats[key]
    return {"connections": edges, "stats": totals, "cost_usd": round(cost, 6)}


# ---- the real model ------------------------------------------------------------


def make_complete(model: str) -> Complete:
    """A ``complete`` that calls Claude: litellm when the workspace has an API key,
    otherwise headless ``claude -p`` on the workspace's sign-in."""
    from claude_p import claude_p_completion, read_workspace_ai_credentials

    def complete(prompt: str, system: str) -> tuple[str, float]:
        creds = read_workspace_ai_credentials()
        if creds.api_key:
            from litellm import completion, completion_cost

            response = completion(
                model=model,
                api_key=creds.api_key,
                api_base=(creds.base_url or "").rstrip("/") or None,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=8000,
            )
            try:
                cost = float(completion_cost(completion_response=response))
            except Exception:  # an unpriced model id still returns its text
                cost = 0.0
            return response.choices[0].message.content or "", cost
        result = claude_p_completion(prompt, system=system, model=model)
        return result.text, float(result.cost_usd or 0.0)

    return complete
