"""Per-note entity/concept extraction for the knowledge galaxy.

Turns one note into many finer-grained nodes (characters, locations, plot
threads, sub-topics — whatever it actually contains) instead of a single
file-level node. Pure prompt-building + response-parsing here; the actual
`claude -p` subprocess call and graph assembly live in server.py, next to
the only other code that shells out to Claude, per this codebase's existing
split (classroom.py/calendar_api.py never call ask_claude themselves either).
"""
import hashlib
import json

MAX_NOTE_CHARS_FOR_EXTRACTION = 4000
MAX_ENTITIES_PER_NOTE = 12


def content_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def build_extraction_prompt(title, text):
    return (
        "You are analyzing one note for a 3D knowledge-graph visualizer. "
        f"Note title: \"{title}\".\n\n"
        "Read its content below and extract the main distinct entities, "
        "concepts, or ideas it actually discusses — could be characters, "
        "locations, plot threads, sub-topics, people, projects, whatever "
        "genuinely appears in THIS note. Skip anything trivial or generic. "
        "Respond with ONLY a raw JSON array — no markdown fences, no extra "
        "text — of objects shaped exactly like:\n"
        '[{"name": "<short entity name, 1-4 words>", "kind": '
        '"<one or two word category, e.g. character/location/topic/concept>", '
        '"excerpt": "<one sentence about this entity, grounded in the note>"}]\n'
        f"Return between 0 and {MAX_ENTITIES_PER_NOTE} entities — 0 only if "
        "the note is genuinely too thin to contain any. Never invent an "
        "entity that isn't actually in the text.\n\n"
        "NOTE CONTENT:\n"
        f"{text[:MAX_NOTE_CHARS_FOR_EXTRACTION]}"
    )


def parse_entities_response(raw_text):
    text = (raw_text or "").strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    out = []
    for item in data[:MAX_ENTITIES_PER_NOTE]:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "kind": (item.get("kind") or "concept").strip() or "concept",
            "excerpt": (item.get("excerpt") or "").strip(),
        })
    return out


def normalize_entity_name(name):
    return name.strip().lower()
