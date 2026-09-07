#!/usr/bin/env python3
"""Tiny standard-library web server for the knowledge galaxy viewer.

Serves viewer/ on port 4700 and answers POST /chat by scoring notes
against the question (keyword overlap) and shelling out to `claude -p`
so answers run on the Claude Code subscription, not a paid API key.
"""
import datetime
import difflib
import email
import http.server
import io
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import zipfile

import build
import calendar_api
import classroom
import documents
import dunk_firebase
import entities
import project_auth
import workspaces

# job_id -> {"status": "pending"|"done"|"error", "message": str, "result": {...}}
# populated by background threads spawned from handle_documents_create
DOCUMENT_JOBS = {}

# item-keys already surfaced as a proactive alert this server run — never
# re-mention the same still-pending item twice (resets on restart, which is
# fine: worst case is one repeat alert per restart, never silence)
SEEN_ALERT_KEYS = set()
ALERT_CLASSROOM_WINDOW = datetime.timedelta(hours=24)
ALERT_CALENDAR_WINDOW = datetime.timedelta(minutes=30)
STALE_PROJECT_DAYS = 7

# active Claude model for every ask_claude() call — switchable via voice/
# text ("cambia al modelo opus"), read by the client to color-code the HUD
ALLOWED_MODELS = {"sonnet", "opus", "haiku"}
ACTIVE_MODEL = "sonnet"

# single-user app: exactly one deletion can be "awaiting password" at a time
PENDING_DELETION = None  # {"slug", "name", "expires_at"}
PENDING_DELETION_TTL_SECONDS = 120

# Cloud hosts provide PORT dynamically.  Keeping the localhost default makes
# local runs private while allowing `python server.py` to work on Render,
# Railway, and similar services without a separate command.
PORT = int(os.environ.get("PORT", "4700"))
HOST = os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIRECTORY = os.path.join(BASE_DIR, "viewer")
MEMORY_CAP = 500
MEMORY_PROMPT_COUNT = 20
PROJECT_NAME_MATCH_CUTOFF = 0.6


# --- active-workspace path resolution — every notes/memory/project-tracker
# path below resolves against whichever workspace is currently active, so
# /chat, /remember, project-tracker, and the Classroom/Calendar conversation
# log all operate on the active project without each handler knowing it ---

def notes_dir():
    return workspaces.paths(workspaces.active_slug())["notes"]


def captures_dir():
    return workspaces.paths(workspaces.active_slug())["captures"]


def memory_path():
    return workspaces.paths(workspaces.active_slug())["memory"]


def projects_path():
    return workspaces.paths(workspaces.active_slug())["projects"]


def graph_path():
    return workspaces.paths(workspaces.active_slug())["graph"]


def workspace_root():
    return workspaces.paths(workspaces.active_slug())["root"]

# read-only file browsing: ONLY these two folders, symlink-safe (realpath),
# never used for writing/editing/deleting/moving anything
FILES_ROOTS = [
    os.path.realpath(os.path.expanduser("~/Desktop")),
    os.path.realpath(os.path.expanduser("~/Documents")),
]
FILES_READ_CAP = 200 * 1024  # 200KB — larger text files are truncated, not refused
FILES_SEARCH_MAX_RESULTS = 50
FILES_SEARCH_MAX_SCAN = 50000  # bail out early on pathologically large folders
FILES_SUMMARY_PROMPT_CAP = 3000  # chars of file content actually sent to claude -p to summarize

FILES_SEARCH_STRIP_RE = re.compile(
    r"^\s*busca\s+(?:el\s+archivo|en\s+mis?\s+documentos?|en\s+mi\s+escritorio)\b[:\s]*",
    re.IGNORECASE,
)
FILES_READ_STRIP_RE = re.compile(
    r"^\s*(?:l[eé]eme|abre)\s+el\s+archivo\b[:\s]*",
    re.IGNORECASE,
)

# hard-coded, not runtime-editable — the ONLY app names ever passed to
# `open -a`. Never pass raw user text to subprocess; only a value drawn
# from this exact list.
ALLOWED_APPS = [
    "Google Chrome", "Notes", "Calendar", "Mail", "Spotify", "Finder",
    "Visual Studio Code", "Safari", "System Settings", "Messages",
    "WhatsApp", "Preview", "Shortcuts", "Claude",
]
APP_ALIASES = {
    "Google Chrome": ["chrome", "google chrome"],
    "Notes": ["notas", "note"],
    "Calendar": ["calendario", "agenda"],
    "Mail": ["correo", "mail", "correo electronico"],
    "Spotify": ["spotify"],
    "Finder": ["finder", "buscador", "explorador de archivos"],
    "Visual Studio Code": ["vscode", "vs code", "visual studio code", "codigo", "code"],
    "Safari": ["safari"],
    "System Settings": ["preferencias", "configuracion", "ajustes", "preferencias del sistema", "configuracion del sistema", "system settings", "system preferences"],
    "Messages": ["mensajes", "imessage", "messages"],
    "WhatsApp": ["whatsapp", "whats", "whats app", "wasap", "guasap"],
    "Preview": ["vista previa", "preview"],
    "Shortcuts": ["atajos", "shortcuts"],
    "Claude": ["claude"],
}
APP_NAME_MATCH_CUTOFF = 0.6
OPEN_APP_STRIP_RE = re.compile(r"^\s*(?:abre|ábreme)\s+", re.IGNORECASE)

REMEMBER_TRIGGER_RE = re.compile(
    r"^\s*(recu[eé]rdate?\s+que|recu[eé]rdame\s+que|recuerda\s+que)\s*[:,]?\s*",
    re.IGNORECASE,
)
PROJECT_UPDATE_RE = re.compile(r"^\s*proyecto\s+([^:]+):\s*(.+)$", re.IGNORECASE | re.DOTALL)
PROJECT_STATUS_KEYWORDS = [
    (re.compile(r"complet|termin|list[oa]", re.IGNORECASE), "Completado"),
    (re.compile(r"pausa|detenid|en\s+espera", re.IGNORECASE), "Pausado"),
    (re.compile(r"cancel", re.IGNORECASE), "Cancelado"),
]
SPANISH_TITLE_LOWER = {
    "de", "del", "la", "el", "los", "las", "en", "y", "que", "un", "una",
    "con", "por", "para", "a", "mi", "su",
}
ACCENT_MAP = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")

BUTLER_PERSONA = (
    "You are Jarvis, the user's butler for this notes archive: dry, "
    "impeccably polite, razor wit. You respond ENTIRELY in Mexican "
    "Spanish — never a word of English. Address the user as \"señor\" "
    "only occasionally, not in every reply. One genuinely funny line "
    "beats three bland ones."
)

TOP_K = 6
MAX_HISTORY_TURNS = 6
TITLE_WEIGHT = 5
EXCERPT_WEIGHT = 1

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "do", "does", "did",
    "what", "how", "why", "who", "which", "when", "where", "to", "of",
    "in", "on", "at", "for", "with", "and", "or", "my", "me", "i", "you",
    "your", "it", "this", "that", "about", "can", "could", "should",
    "would", "tell", "explain", "note", "notes", "please", "give", "us",
    "we", "our", "be", "have", "has", "had", "will", "just", "so", "as",
    # Spanish
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "que", "qué", "cómo", "como", "dónde", "donde", "cuándo", "cuando",
    "cuál", "cual", "quién", "quien", "por", "para", "es", "son", "está",
    "esta", "están", "estan", "en", "con", "sobre", "y", "o", "mi", "me",
    "tu", "te", "yo", "nos", "cuéntame", "cuentame", "dime", "hazme",
}

TOKEN_RE = re.compile(r"[a-z0-9áéíóúüñ']+")

# session_id -> list of {"question": str, "answer": str}
SESSIONS = {}


def load_memory():
    path = memory_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def append_memory(question, answer, lang):
    path = memory_path()
    entries = load_memory()
    entries.append({
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "question": question,
        "answer": answer,
        "lang": lang,
    })
    entries = entries[-MEMORY_CAP:]  # cap growth: keep only the most recent MEMORY_CAP exchanges
    # write atomically so a crash mid-write can't corrupt the log
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def recent_memory(limit=MEMORY_PROMPT_COUNT, exclude=None):
    """Last `limit` persisted exchanges, excluding any already shown via
    exclude (the current session's own in-memory history) to avoid showing
    the model the same turns twice in one prompt."""
    exclude_pairs = {(h["question"], h["answer"]) for h in (exclude or [])}
    recent = load_memory()[-limit:]
    return [m for m in recent if (m["question"], m["answer"]) not in exclude_pairs]


def load_projects():
    path = projects_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_projects(projects):
    path = projects_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def normalize_project_name(name):
    return name.translate(ACCENT_MAP).lower().strip()


def project_id_from_name(name, existing_ids):
    base = slugify(name).lower() or "proyecto"
    candidate = base
    n = 2
    while candidate in existing_ids:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def find_project(projects, name):
    target = normalize_project_name(name)
    normalized = [normalize_project_name(p["name"]) for p in projects]
    matches = difflib.get_close_matches(target, normalized, n=1, cutoff=PROJECT_NAME_MATCH_CUTOFF)
    if not matches:
        return None
    return projects[normalized.index(matches[0])]


def infer_status(update_text, current_status):
    for pattern, status in PROJECT_STATUS_KEYWORDS:
        if pattern.search(update_text):
            return status
    return current_status


def upsert_project(name, update_text):
    projects = load_projects()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    project = find_project(projects, name)
    if project is None:
        project = {
            "id": project_id_from_name(name, {p["id"] for p in projects}),
            "name": name.strip(),
            "status": infer_status(update_text, "En curso"),
            "notes": [],
            "created": now,
            "updated": now,
        }
        projects.append(project)
    else:
        project["status"] = infer_status(update_text, project["status"])
        project["updated"] = now
    project["notes"].append({"timestamp": now, "text": update_text})
    save_projects(projects)
    return project


def build_project_confirm_prompt(project, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user just gave a status update on their project \"{project['name']}\" "
        f"(status now: {project['status']}). Reply with ONE witty sentence, in "
        "character, confirming the update is logged. Do not repeat the update's "
        "content verbatim — just the confirmation."
    )
    return "\n".join(lines)


def build_project_summary_prompt(projects, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        "The user asked for a status summary of their tracked projects. For "
        "EACH project below, give 1-2 witty-but-informative spoken sentences: "
        "its name, its status, and the gist of its most recent update. Keep "
        "each project tight — this will be read aloud. If there are no "
        "projects, say so playfully, still in character."
    )
    lines.append("")
    if projects:
        lines.append("PROJECTS:")
        for p in projects:
            last = p["notes"][-1]["text"] if p["notes"] else "(sin actualizaciones aún)"
            lines.append(f"- {p['name']} | status: {p['status']} | última actualización: {last}")
    else:
        lines.append("PROJECTS: (ninguno todavía)")
    return "\n".join(lines)


def build_classroom_prompt(courses, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        "The user asked what Classroom assignments they have pending "
        "(not yet submitted or graded). Below is the real list, grouped "
        "by course. Read it back grouped BY COURSE, in Spanish: for each "
        "course say its name, then its pending assignment titles with "
        "due dates. Keep it tight and witty in character — this will be "
        "read aloud — but do not drop or invent any assignment. If there "
        "are no pending assignments at all, say so playfully."
    )
    lines.append("")
    if courses:
        lines.append("PENDING ASSIGNMENTS BY COURSE:")
        for c in courses:
            lines.append(f"- {c['course_name']}:")
            for a in c["assignments"]:
                lines.append(f"  - {a['title']} (vence: {a['due_label']})")
    else:
        lines.append("PENDING ASSIGNMENTS BY COURSE: (ninguna, todo al día)")
    return "\n".join(lines)


def build_classroom_error_prompt(reason, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked for their Classroom assignments, but they could "
        f"not be fetched ({reason}). Explain this in ONE witty in-character "
        "sentence, in Spanish — no raw error text, no technical jargon."
    )
    return "\n".join(lines)


def build_calendar_prompt(days_groups, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        "The user asked what's on their calendar. Below are their upcoming "
        "events for the next days, grouped by day. Read it back grouped BY "
        "DAY, in Spanish: for each day say the day label, then each "
        "event's time and title. Keep it tight and witty in character — "
        "this will be read aloud — but do not drop or invent any event. "
        "If there are no upcoming events at all, say so playfully."
    )
    lines.append("")
    if days_groups:
        lines.append("UPCOMING EVENTS BY DAY:")
        for d in days_groups:
            lines.append(f"- {d['day_label']}:")
            for e in d["events"]:
                lines.append(f"  - {e['time_label']}: {e['summary']}")
    else:
        lines.append("UPCOMING EVENTS BY DAY: (ninguno, agenda libre)")
    return "\n".join(lines)


def build_calendar_error_prompt(reason, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked about their calendar (or to schedule something), "
        f"but it could not be completed ({reason}). Explain this in ONE "
        "witty in-character sentence, in Spanish — no raw error text, no "
        "technical jargon."
    )
    return "\n".join(lines)


def _now_label_es():
    now = datetime.datetime.now()
    return (
        f"{calendar_api.DAY_NAMES_ES[now.weekday()]} {now.day} de "
        f"{calendar_api.MONTH_NAMES_ES[now.month]} de {now.year}, "
        f"{now.strftime('%H:%M')}"
    )


def build_calendar_extract_prompt(raw_text, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"Right now it is {_now_label_es()} (local time). The user just "
        f"asked you to schedule something: \"{raw_text}\". Extract the "
        "event title and the exact date/time they mean, resolving any "
        "relative reference (\"mañana\", \"el viernes\", \"en una hora\") "
        "against the current moment above. Respond with ONLY a raw JSON "
        "object — no markdown fences, no extra text — in EXACTLY one of "
        "these two shapes:\n"
        '{"ok": true, "title": "<event title>", '
        '"start": "<YYYY-MM-DDTHH:MM:SS>", '
        '"duration_minutes": <int, default 60 if not stated>}\n'
        "or, if the date/time or the event itself is genuinely ambiguous "
        "or missing:\n"
        '{"ok": false, "clarification_question": '
        '"<ONE short witty in-character question, in Spanish, asking '
        'exactly what is missing>"}\n'
        "Never guess a date/time you are not confident about — ask "
        "instead."
    )
    return "\n".join(lines)


def build_calendar_create_confirm_prompt(event, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"You just created a calendar event titled \"{event['summary']}\" "
        f"starting {event['start_label']} and ending at {event['end_label']}. "
        "Confirm it in ONE witty in-character sentence, in Spanish, and you "
        "MUST state the exact day and time back to the user so they can "
        "verify it's correct — do not omit it."
    )
    return "\n".join(lines)


def _extract_json_object(text):
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in model output: " + text[:200])
    return json.loads(text[start:end + 1])


def build_workspace_summary_prompt(name, sample_notes):
    lines = [BUTLER_PERSONA]
    lines.append("")
    lines.append(
        f"The user just dropped a new folder of notes named \"{name}\" into "
        "Jarvis, creating a brand-new independent project. Below are "
        "excerpts from a few of its notes. In ONE sentence, in Spanish, "
        "give a genuinely informative sense of what this content seems to "
        "be about — witty in character, not generic filler."
    )
    lines.append("")
    if sample_notes:
        lines.append("SAMPLE NOTES:")
        for n in sample_notes:
            lines.append(f"- [{n['label']}]: {n['excerpt'][:300]}")
    else:
        lines.append("SAMPLE NOTES: (none readable)")
    return "\n".join(lines)


def build_workspace_switch_prompt(workspace_row, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user just asked you to switch focus to their project "
        f"\"{workspace_row['name']}\". Confirm it in ONE short witty "
        "in-character sentence, in Spanish, naming the project."
    )
    return "\n".join(lines)


def build_workspace_error_prompt(reason, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user tried to switch or drop in a project, but it failed "
        f"({reason}). Explain this in ONE witty in-character sentence, in "
        "Spanish — no raw error text, no technical jargon."
    )
    return "\n".join(lines)


def sanitize_relative_path(raw_path):
    """Collapse a possibly-hostile relative path (from a dropped folder or a
    zip entry) down to safe path segments — drops '..'/'.'/empty segments
    entirely rather than rejecting the whole file, and never allows escaping
    the destination directory (zip-slip prevention)."""
    raw_path = (raw_path or "").replace("\\", "/")
    parts = [p for p in raw_path.split("/") if p not in ("", ".", "..")]
    if not parts:
        return None
    return os.path.join(*parts)


def extract_zip_safely(content_bytes):
    """Returns [(relative_path, bytes), ...] for every .md entry in the zip,
    with every entry name sanitized against zip-slip."""
    result = []
    with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = sanitize_relative_path(info.filename)
            if not rel or not rel.lower().endswith(".md"):
                continue
            if "__MACOSX" in rel.split(os.sep) or os.path.basename(rel) == ".DS_Store":
                continue
            result.append((rel, zf.read(info)))
    return result


def common_top_folder(note_files):
    tops = {rel.split(os.sep)[0] for rel, _content in note_files if os.sep in rel}
    return next(iter(tops)) if len(tops) == 1 else None


def default_project_name():
    return "Proyecto sin título " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def parse_multipart_form(content_type_header, body):
    """Stdlib-only multipart/form-data parser (the old `cgi.FieldStorage`
    trick is gone — `cgi` was removed in Python 3.13). Multipart bodies are
    structurally MIME messages, so we hand the email package a synthetic
    header + the raw body and let it split the parts for us.
    Returns (fields: dict[str, str], files: [(field_name, filename, bytes)])."""
    header_bytes = f"Content-Type: {content_type_header}\r\n\r\n".encode("utf-8")
    msg = email.message_from_bytes(header_bytes + body)
    fields = {}
    files = []
    if not msg.is_multipart():
        return fields, files
    for part in msg.get_payload():
        disposition = part.get("Content-Disposition", "")
        if not disposition:
            continue
        name_match = re.search(r'name="([^"]*)"', disposition)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        name = name_match.group(1) if name_match else None
        payload = part.get_payload(decode=True) or b""
        if filename_match and filename_match.group(1):
            files.append((name, filename_match.group(1), payload))
        elif name:
            fields[name] = payload.decode("utf-8", errors="replace")
    return fields, files


def _load_entities_cache(cache_path):
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_entities_cache(cache_path, cache):
    tmp_path = cache_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, cache_path)


def _kind_bucket(kind):
    # claude's own "kind" wording varies call to call ("plot" vs "plot
    # thread") — bucket on the first word so containment-merging below
    # isn't blocked by that noise
    return (kind or "concept").strip().lower().split()[0]


def _find_containing_entity_id(norm_name, kind, entity_name_to_id, entity_id_kind):
    """Same-category entities where one name is a whole-word substring of
    the other (e.g. "Elena" / "Elena Vasquez") are almost always the same
    entity extracted slightly differently across independent per-note
    extraction calls — merge them into one node instead of duplicating."""
    bucket = _kind_bucket(kind)
    for existing_norm, eid in entity_name_to_id.items():
        if _kind_bucket(entity_id_kind.get(eid)) != bucket:
            continue
        if re.search(r"\b" + re.escape(norm_name) + r"\b", existing_norm):
            return eid
        if re.search(r"\b" + re.escape(existing_norm) + r"\b", norm_name):
            return eid
    return None


def enrich_graph(notes_dir_path, workspace_root_path, project_name):
    """Runs right after build.build_graph() has written the plain
    file-level graph.json for a workspace. Extracts entities/concepts out of
    each note (cached per unchanged file content, so re-ingesting or adding
    one more note doesn't re-extract everything), merges same-named entities
    across notes into shared nodes, links entities mentioned together in the
    same note (and cross-note, by name-mention — same technique build.py
    already uses for file titles), and adds one big "project core" node
    every file node links back to. Rewrites graph.json in place."""
    graph_path_ = os.path.join(workspace_root_path, "graph.json")
    with open(graph_path_, "r", encoding="utf-8") as f:
        graph = json.load(f)

    file_nodes = graph["nodes"]
    file_links = graph["links"]

    cache_path = os.path.join(workspace_root_path, "entities_cache.json")
    cache = _load_entities_cache(cache_path)
    new_cache = {}

    note_paths = build.find_notes(notes_dir_path)

    entity_nodes = []
    entity_name_to_id = {}
    entity_id_kind = {}
    file_id_to_entity_ids = {}
    file_id_to_text = {}
    next_id = len(file_nodes)

    for path in note_paths:
        label = build.title_from_filename(os.path.basename(path))
        file_node = next((n for n in file_nodes if n["label"] == label), None)
        if file_node is None:
            continue  # note somehow didn't produce a node — skip, never crash ingestion over it

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw_text = f.read()
        except OSError:
            continue
        text = build.strip_markdown(raw_text)
        file_id_to_text[file_node["id"]] = text
        h = entities.content_hash(text)

        cached = cache.get(path)
        if cached and cached.get("hash") == h:
            extracted = cached["entities"]
        else:
            try:
                extracted = entities.parse_entities_response(
                    ask_claude(entities.build_extraction_prompt(label, text)))
            except Exception:
                traceback.print_exc()
                extracted = []
        new_cache[path] = {"hash": h, "entities": extracted}

        this_note_ids = []
        for ent in extracted:
            norm = entities.normalize_entity_name(ent["name"])
            eid = entity_name_to_id.get(norm)
            if eid is None:
                eid = _find_containing_entity_id(norm, ent["kind"], entity_name_to_id, entity_id_kind)
            if eid is None:
                eid = next_id
                next_id += 1
                entity_id_kind[eid] = ent["kind"]
                entity_nodes.append({
                    "id": eid,
                    "label": ent["name"],
                    "group": ent["kind"],
                    "excerpt": ent["excerpt"],
                    "__entity": True,
                })
            entity_name_to_id[norm] = eid  # register this alias too — future exact matches hit the fast path
            this_note_ids.append(eid)
        file_id_to_entity_ids[file_node["id"]] = this_note_ids

    _save_entities_cache(cache_path, new_cache)

    link_set = {tuple(sorted((l["source"], l["target"]))) for l in file_links}
    new_links = []

    def add_link(a, b):
        if a == b:
            return
        key = tuple(sorted((a, b)))
        if key in link_set:
            return
        link_set.add(key)
        new_links.append({"source": key[0], "target": key[1]})

    for file_id, ent_ids in file_id_to_entity_ids.items():
        for eid in ent_ids:
            add_link(file_id, eid)          # entity <-> the note it came from
        for i in range(len(ent_ids)):
            for j in range(i + 1, len(ent_ids)):
                add_link(ent_ids[i], ent_ids[j])  # co-mentioned in the same note

    # cross-note entity mentions — same whole-word technique build.py uses
    # for file-title mentions, applied at entity-name granularity
    for file_id, text in file_id_to_text.items():
        text_lower = text.lower()
        home_ids = set(file_id_to_entity_ids.get(file_id, []))
        for norm_name, eid in entity_name_to_id.items():
            if eid in home_ids:
                continue
            if re.search(r"\b" + re.escape(norm_name) + r"\b", text_lower):
                add_link(file_id, eid)

    # the project core: one node every file links back to, so the galaxy
    # visually radiates from it
    core_id = next_id
    core_node = {
        "id": core_id,
        "label": project_name,
        "group": "core",
        "excerpt": f"Núcleo del proyecto «{project_name}».",
        "__core": True,
    }
    for n in file_nodes:
        add_link(core_id, n["id"])

    graph["nodes"] = file_nodes + entity_nodes + [core_node]
    graph["links"] = file_links + new_links

    with open(graph_path_, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)

    return graph


DOCUMENT_KIND_LABEL_ES = {"receipt": "recibo", "report": "reporte", "letter": "carta"}


def build_document_confirm_prompt(kind, result, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    extra = f" por un total de ${result['total']:.2f}" if kind == "receipt" else ""
    lines.append(
        f"You just finished drafting a {DOCUMENT_KIND_LABEL_ES[kind]} "
        f"titled \"{result['title']}\"{extra}, and saved it as "
        f"\"{result['filename']}\" inside the \"{result['folder']}\" folder. "
        "Confirm it in ONE witty in-character sentence, in Spanish, and you "
        "MUST state the exact filename and folder so the user can find it "
        "— do not omit them."
    )
    return "\n".join(lines)


def build_document_error_prompt(kind, reason, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked you to draft a {DOCUMENT_KIND_LABEL_ES.get(kind, kind)} "
        f"document, but it could not be completed ({reason}). Explain this "
        "in ONE witty in-character sentence, in Spanish — no raw error "
        "text, no technical jargon."
    )
    return "\n".join(lines)


def build_dunk_stats_prompt(stats, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        "The user asked how their game DUNK's players are doing. Below is "
        "REAL data pulled just now from Firestore: how many player "
        "profiles were updated in the last 24 hours, each active player's "
        "CURRENT save-state, and the top 5 scores overall for context. "
        "There is no historical snapshot to diff against, so this is a "
        "CURRENT STATE, never a 24h progress delta — do not claim any "
        "specific amount of progress was 'gained' today, and if it isn't "
        "already obvious, say plainly that this is today's snapshot, not "
        "a change-over-time figure. Read it back in Spanish: the active "
        "count, a sense of each active player's current state, and the "
        "top scores. If the active count is 0, say so plainly and still "
        "give the top scores for context. Witty in character, but keep "
        "the actual numbers accurate — do not invent or round dishonestly."
    )
    lines.append("")
    lines.append(f"JUGADORES ACTIVOS (perfil actualizado en las últimas 24h): {stats['active_count']}")
    if stats["saves_summary"]:
        lines.append("ESTADO ACTUAL DE JUGADORES ACTIVOS:")
        for s in stats["saves_summary"]:
            lines.append(f"- {s['username']}: {s['summary']}")
    lines.append("TOP 5 PUNTAJES (histórico, no solo hoy):")
    for i, s in enumerate(stats["top_scores"], 1):
        lines.append(f"{i}. {s['username']}: {s['score']}")
    return "\n".join(lines)


def build_dunk_stats_error_prompt(reason, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked about their game DUNK's player stats, but it "
        f"could not be fetched from Firestore ({reason}). Explain this in "
        "ONE witty in-character sentence, in Spanish — no raw error text, "
        "no technical jargon."
    )
    return "\n".join(lines)


def _new_urgent_alerts():
    """Returns a list of short Spanish fact-strings for items that just
    crossed into 'urgent' — a Classroom deadline within 24h, or a calendar
    event starting within 30 minutes — and have not already been surfaced
    this server run. Never raises: any single source failing just means
    that source contributes nothing this check, it never blocks the other."""
    now = datetime.datetime.now().astimezone()
    messages = []

    try:
        courses = classroom.get_pending_assignments()
    except Exception:
        courses = []
    for c in courses:
        for a in c.get("assignments", []):
            due_iso = a.get("due_iso")
            if not due_iso:
                continue
            try:
                due_dt = datetime.datetime.fromisoformat(due_iso)
            except ValueError:
                continue
            if due_dt.tzinfo is None:
                due_dt = due_dt.replace(tzinfo=now.tzinfo)
            delta = due_dt - now
            if datetime.timedelta(0) <= delta <= ALERT_CLASSROOM_WINDOW:
                key = f"classroom:{c.get('course_name')}:{a.get('title')}:{due_iso}"
                if key not in SEEN_ALERT_KEYS:
                    SEEN_ALERT_KEYS.add(key)
                    messages.append(
                        f"la tarea «{a.get('title')}» de {c.get('course_name')} "
                        f"vence pronto ({a.get('due_label')})"
                    )

    try:
        calendar_days = calendar_api.get_upcoming_events(days=1)
    except Exception:
        calendar_days = []
    for day in calendar_days:
        for ev in day.get("events", []):
            start_iso = ev.get("start_iso")
            if not start_iso:
                continue
            try:
                start_dt = datetime.datetime.fromisoformat(start_iso)
            except ValueError:
                continue
            delta = start_dt - now
            if datetime.timedelta(0) <= delta <= ALERT_CALENDAR_WINDOW:
                key = f"calendar:{ev.get('summary')}:{start_iso}"
                if key not in SEEN_ALERT_KEYS:
                    SEEN_ALERT_KEYS.add(key)
                    messages.append(f"«{ev.get('summary')}» empieza en breve ({ev.get('time_label')})")

    return messages


def build_alert_prompt(items):
    lines = [BUTLER_PERSONA]
    lines.append("")
    lines.append(
        "Something time-sensitive just came up (a Classroom deadline "
        "within 24h, or a calendar event starting within 30 minutes). "
        "Mention it in ONE short, urgent-but-witty sentence, in Spanish, "
        "as an unprompted heads-up — this will be prepended before "
        "whatever else you say next, so keep it brief and self-contained."
    )
    lines.append("")
    lines.append("ITEMS:")
    for item in items:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _stale_or_paused_projects():
    now = datetime.datetime.now()
    result = []
    for p in load_projects():
        is_paused = p.get("status") == "Pausado"
        is_stale = False
        updated_str = p.get("updated")
        if updated_str:
            try:
                updated_dt = datetime.datetime.fromisoformat(updated_str)
                is_stale = (now - updated_dt) > datetime.timedelta(days=STALE_PROJECT_DAYS)
            except ValueError:
                pass
        if is_paused or is_stale:
            result.append(p)
    return result


def build_briefing_prompt(courses, calendar_days, stale_projects, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        "Give the user their morning briefing. Combine pending Classroom "
        "assignments due soon, today's calendar events, and any tracked "
        "project that's paused or hasn't been updated in a while, into "
        "ONE prioritized spoken summary — most time-sensitive first, a "
        "few sentences total, in Spanish, witty in character. If NONE of "
        "the three sections below has anything noteworthy, say so briefly "
        "instead of forcing a report — do not pad it out or invent "
        "urgency that isn't there."
    )
    lines.append("")
    if courses:
        lines.append("TAREAS DE CLASSROOM PENDIENTES:")
        for c in courses:
            for a in c.get("assignments", []):
                lines.append(f"- {a.get('title')} ({c.get('course_name')}) vence: {a.get('due_label')}")
    else:
        lines.append("TAREAS DE CLASSROOM PENDIENTES: (ninguna)")
    lines.append("")
    if calendar_days:
        lines.append("EVENTOS DE CALENDARIO (próximas 24h):")
        for day in calendar_days:
            for ev in day.get("events", []):
                lines.append(f"- {ev.get('time_label')}: {ev.get('summary')}")
    else:
        lines.append("EVENTOS DE CALENDARIO (próximas 24h): (ninguno)")
    lines.append("")
    if stale_projects:
        lines.append("PROYECTOS PAUSADOS O DESATENDIDOS:")
        for p in stale_projects:
            lines.append(f"- {p.get('name')} (status: {p.get('status')}, última actualización: {p.get('updated')})")
    else:
        lines.append("PROYECTOS PAUSADOS O DESATENDIDOS: (ninguno)")
    return "\n".join(lines)


def build_delete_needs_password_prompt(name, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked to delete the project \"{name}\" — a destructive, "
        "irreversible action, so it's gated behind a password. Ask them "
        "for the password now, in ONE short witty in-character sentence, "
        "in Spanish. Do not proceed, and do not hint at what the password "
        "might be."
    )
    return "\n".join(lines)


def build_delete_success_prompt(name, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The correct password was just given, and the project \"{name}\" "
        "has been permanently deleted. Confirm it in ONE witty "
        "in-character sentence, in Spanish."
    )
    return "\n".join(lines)


def build_delete_wrong_password_prompt(memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        "The user just gave the WRONG password for a pending project "
        "deletion. Refuse politely in ONE witty in-character sentence, in "
        "Spanish — do not confirm or deny anything about what the "
        "correct password is, and do not proceed with any deletion."
    )
    return "\n".join(lines)


def build_delete_error_prompt(reason, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked to delete a project, but it could not proceed "
        f"({reason}). Explain this in ONE witty in-character sentence, in "
        "Spanish — no raw error text, no technical jargon."
    )
    return "\n".join(lines)


def build_model_switch_prompt(model, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user just asked you to switch to the \"{model}\" Claude "
        "model — you are now actually running as that model. Confirm it "
        "in ONE short witty in-character sentence, in Spanish, naming the "
        "model."
    )
    return "\n".join(lines)


def build_model_switch_error_prompt(reason, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked to switch Claude models, but it failed ({reason}). "
        "Explain this in ONE witty in-character sentence, in Spanish — no "
        "raw error text."
    )
    return "\n".join(lines)


def build_file_search_prompt(query, results, truncated, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked you to search their files for \"{query}\" (inside "
        "~/Desktop and ~/Documents only). Read back the matching filenames "
        "below in 1-3 witty spoken sentences, in Spanish, in character. If "
        "none were found, say so plainly, still witty. Do not invent "
        "filenames that aren't listed."
    )
    lines.append("")
    if results:
        lines.append("MATCHES:")
        for r in results[:15]:
            lines.append(f"- {r['name']}")
        if truncated:
            lines.append("(search stopped early — there may be more matches)")
    else:
        lines.append("MATCHES: (none)")
    return "\n".join(lines)


def build_file_read_error_prompt(filename, reason, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked you to read/open the file \"{filename}\", but it "
        f"could not be shown ({reason}). Explain this to them in ONE witty "
        "in-character sentence, in Spanish — no raw error text, no "
        "technical jargon."
    )
    return "\n".join(lines)


def build_file_read_summary_prompt(filename, content, read_truncated, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked you to read/open the file \"{filename}\". Below is "
        "its content (or the start of it). Summarize what's actually IN it "
        "for them in 2-3 spoken sentences, in Spanish, witty but genuinely "
        "informative — don't just say 'here is the file'. The full text is "
        "already shown on screen, so don't recite it verbatim."
    )
    lines.append("")
    lines.append("CONTENT:")
    lines.append(content[:FILES_SUMMARY_PROMPT_CAP])
    if read_truncated or len(content) > FILES_SUMMARY_PROMPT_CAP:
        lines.append("(content was truncated)")
    return "\n".join(lines)


def resolve_under_files_roots(path_str):
    """Return the realpath of path_str if — after resolving symlinks — it
    falls inside one of FILES_ROOTS, else None. realpath() on both sides
    means a symlink inside Desktop/Documents that points elsewhere on disk
    can't be used to read outside the allowed folders."""
    if not path_str:
        return None
    try:
        candidate = os.path.realpath(os.path.expanduser(path_str))
    except (OSError, ValueError):
        return None
    for root in FILES_ROOTS:
        if candidate == root or candidate.startswith(root + os.sep):
            return candidate
    return None


def is_hidden(name):
    return name.startswith(".")


def search_files(query, max_results=FILES_SEARCH_MAX_RESULTS):
    query_lower = query.lower()
    results = []
    scanned = 0
    for root in FILES_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not is_hidden(d)]
            for fname in filenames:
                if is_hidden(fname):
                    continue
                scanned += 1
                if scanned > FILES_SEARCH_MAX_SCAN:
                    return results, True
                if query_lower in fname.lower():
                    results.append({"path": os.path.join(dirpath, fname), "name": fname})
                    if len(results) >= max_results:
                        return results, False
    return results, False


def read_text_file(resolved):
    """Read an already-validated path as text.
    Returns (text, size, truncated, error) — error is None on success, else
    one of 'not_found' / 'io_error' / 'binary'."""
    if not os.path.isfile(resolved):
        return None, None, None, "not_found"
    try:
        size = os.path.getsize(resolved)
        with open(resolved, "rb") as f:
            raw = f.read(FILES_READ_CAP + 1)
    except OSError:
        return None, None, None, "io_error"
    truncated = len(raw) > FILES_READ_CAP
    raw = raw[:FILES_READ_CAP]
    if b"\x00" in raw:
        return None, None, None, "binary"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, None, "binary"
    return text, size, truncated, None


def normalize_app_query(text):
    return text.translate(ACCENT_MAP).lower().strip()


_APP_TERMS = {}
for _canonical in ALLOWED_APPS:
    _APP_TERMS[normalize_app_query(_canonical)] = _canonical
for _canonical, _aliases in APP_ALIASES.items():
    for _alias in _aliases:
        _APP_TERMS[normalize_app_query(_alias)] = _canonical


def match_allowed_app(query):
    """Resolve free-text/voice input to one of the ALLOWED_APPS canonical
    names, or None if nothing matches closely enough. The return value is
    ALWAYS either None or a string drawn verbatim from ALLOWED_APPS — never
    the raw query — since that return value is what gets passed to `open -a`."""
    q = normalize_app_query(query)
    if not q:
        return None
    if q in _APP_TERMS:
        return _APP_TERMS[q]
    for term, canonical in _APP_TERMS.items():
        if term and (term in q or q in term):
            return canonical
    matches = difflib.get_close_matches(q, _APP_TERMS.keys(), n=1, cutoff=APP_NAME_MATCH_CUTOFF)
    if matches:
        return _APP_TERMS[matches[0]]
    return None


def build_open_app_confirm_prompt(app_name, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user just asked you to open the app \"{app_name}\" and it "
        "opened successfully. Confirm it in ONE short witty in-character "
        "sentence, in Spanish (e.g. in the spirit of 'Abriendo Spotify, "
        "señor.')."
    )
    return "\n".join(lines)


def build_open_app_refusal_prompt(query, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked you to open \"{query}\", but that app is not on "
        "your fixed approved list (Google Chrome, Notes, Calendar, Mail, "
        "Spotify, Finder, Visual Studio Code, Safari, System Settings, "
        "Messages, WhatsApp, Preview, Shortcuts, Claude). Explain in ONE "
        "witty in-character sentence, in Spanish, that you can only open "
        "apps from your approved list — no raw error text."
    )
    return "\n".join(lines)


def build_open_app_error_prompt(app_name, reason, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user asked you to open \"{app_name}\" (an approved app), but "
        f"it failed to open ({reason} — possibly not installed on this "
        "machine). Explain this in ONE witty in-character sentence, in "
        "Spanish, without raw error text."
    )
    return "\n".join(lines)


def load_graph():
    with open(graph_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def tokenize(text):
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS and len(t) > 2]


def score_notes(question, nodes):
    q_tokens = tokenize(question)
    if not q_tokens:
        return []
    # whole-word matching (not substring) so short fragments can't false-hit
    # random English words inside note excerpts
    patterns = [re.compile(r"\b" + re.escape(t) + r"\b") for t in q_tokens]
    scored = []
    for node in nodes:
        title = node["label"].lower()
        excerpt = node["excerpt"].lower()
        score = 0
        for pattern in patterns:
            score += len(pattern.findall(title)) * TITLE_WEIGHT
            score += len(pattern.findall(excerpt)) * EXCERPT_WEIGHT
        if score > 0:
            scored.append((score, node))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [node for _score, node in scored[:TOP_K]]


def strip_remember_trigger(text):
    return REMEMBER_TRIGGER_RE.sub("", text, count=1).strip()


def make_title(body_text, max_words=6):
    words = body_text.split()[:max_words]
    titled = []
    for i, w in enumerate(words):
        core = w.strip(".,!?;:¿¡")
        if not core:
            continue
        if i > 0 and core.lower() in SPANISH_TITLE_LOWER:
            titled.append(core.lower())
        else:
            titled.append(core[:1].upper() + core[1:])
    return " ".join(titled) or "Nota"


def slugify(text):
    # keep the title's original casing — the filename IS the label (via
    # build.title_from_filename), so lowercasing here would lowercase the
    # note's title everywhere it's displayed
    ascii_text = text.translate(ACCENT_MAP)
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-")
    return ascii_text[:60] or "Nota"


def unique_capture_path(slug):
    # a chronological prefix guarantees captures always sort — and therefore
    # get assigned ids — in creation order, even across many /remember calls;
    # build.title_from_filename() strips it back off for display
    captures = captures_dir()
    os.makedirs(captures, exist_ok=True)
    base = time.strftime("%Y%m%d-%H%M%S") + "-" + slug
    n = 1
    while True:
        fname = f"{base}.md" if n == 1 else f"{base}-{n}.md"
        path = os.path.join(captures, fname)
        if not os.path.exists(path):
            return path
        n += 1


def format_memory_section(memory_entries, heading="EARLIER CONVERSATIONS (other sessions/days)"):
    if not memory_entries:
        return []
    lines = ["", f"{heading}:"]
    for m in memory_entries:
        lines.append(f"[{m.get('timestamp', '?')}] Q: {m.get('question', '')}")
        lines.append(f"A: {m.get('answer', '')}")
    return lines


def build_confirm_prompt(title, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(
        f"The user just asked you to remember something, and you filed it "
        f"away as a new note titled \"{title}\". Reply with ONE witty "
        "sentence, in character, confirming it is safely filed. Do not "
        "describe or repeat the note's content — just the confirmation."
    )
    return "\n".join(lines)


def build_prompt(question, top_notes, history, memory_entries=None):
    lines = [BUTLER_PERSONA]
    lines.append(
        "If the NOTES below are relevant to the question: open with one "
        "witty sentence, then state the facts plainly in 1-2 more "
        "sentences. Never recite a note back verbatim — it is already "
        "showing on screen, just reference it. If the notes don't cover "
        "the question, say so plainly, still in character. If this is "
        "small talk, a joke, or has nothing to do with the notes, just "
        "banter back in character and do not mention the notes at all. "
        "Keep the whole reply to 1-3 sentences, always in Spanish."
    )
    lines.append("")
    if top_notes:
        lines.append("NOTES:")
        for node in top_notes:
            lines.append(f"[{node['label']}] ({node['group']}): {node['excerpt']}")
    else:
        lines.append("NOTES: (no matching notes were found for this question)")
    if history:
        lines.append("")
        lines.append("CONVERSATION SO FAR (this session):")
        for turn in history[-MAX_HISTORY_TURNS:]:
            lines.append(f"Q: {turn['question']}")
            lines.append(f"A: {turn['answer']}")
    lines.extend(format_memory_section(memory_entries or []))
    lines.append("")
    lines.append(f"QUESTION: {question}")
    lines.append("ANSWER:")
    return "\n".join(lines)


def ask_claude(prompt):
    cmd = ["claude", "-p", "--model", ACTIVE_MODEL, "--safe-mode", prompt]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        print("[ask_claude] `claude` CLI not found on PATH", file=sys.stderr, flush=True)
        return "Error: the `claude` CLI was not found on PATH."
    except subprocess.TimeoutExpired:
        print("[ask_claude] `claude` timed out after 60s", file=sys.stderr, flush=True)
        return "Error: claude took too long to respond."
    except Exception:
        # never let an unexpected failure get swallowed into a generic string —
        # dump the real traceback to the server terminal
        print("[ask_claude] unexpected exception running claude:", file=sys.stderr, flush=True)
        traceback.print_exc()
        return "Error: unexpected failure running claude (see server terminal)."

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()

    if result.returncode != 0:
        # the claude CLI writes auth/usage failures to STDOUT, not stderr, and
        # still exits non-zero — so surface both streams, loudly, on the server
        detail = err or out or "no output on stdout or stderr"
        print(
            "[ask_claude] claude exited "
            f"{result.returncode}\n"
            f"  cmd    : claude -p --model {ACTIVE_MODEL} --safe-mode <prompt>\n"
            f"  stdout : {out!r}\n"
            f"  stderr : {err!r}",
            file=sys.stderr,
            flush=True,
        )
        low = detail.lower()
        if "oauth" in low or "authenticate" in low or "login" in low:
            return (
                "Error: la sesión del CLI de claude caducó "
                f"(exit {result.returncode}: {detail}). "
                "Reautentícate con `claude /login` o `claude setup-token`."
            )
        return f"Error running claude (exit {result.returncode}): {detail}"

    if not out:
        # exit 0 but nothing printed — also previously invisible
        print(
            f"[ask_claude] claude exited 0 with empty stdout; stderr={err!r}",
            file=sys.stderr,
            flush=True,
        )
        return f"Error: claude returned no output. stderr: {err or '(empty)'}"

    return out


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        # this viewer is actively edited during development — never let the
        # browser silently serve a stale cached copy on a normal refresh
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/memory-summary":
            self.handle_memory_summary()
        elif path == "/projects":
            self.send_json(200, load_projects())
        elif path == "/graph-data":
            self.handle_graph_data(query)
        elif path == "/workspaces":
            self.handle_workspaces_list()
        elif path == "/documents/status":
            self.handle_documents_status(query)
        elif path == "/alerts/check":
            self.handle_alerts_check()
        elif path == "/model":
            self.send_json(200, {"model": ACTIVE_MODEL})
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/chat":
            self.handle_chat()
        elif self.path == "/remember":
            self.handle_remember()
        elif self.path == "/project-update":
            self.handle_project_update()
        elif self.path == "/project-summary":
            self.handle_project_summary()
        elif self.path == "/classroom-assignments":
            self.handle_classroom_assignments()
        elif self.path == "/calendar-events":
            self.handle_calendar_events()
        elif self.path == "/calendar-create":
            self.handle_calendar_create()
        elif self.path == "/files-search":
            self.handle_files_search()
        elif self.path == "/files-read":
            self.handle_files_read()
        elif self.path == "/open-app":
            self.handle_open_app()
        elif self.path == "/workspaces/ingest":
            self.handle_workspace_ingest()
        elif self.path == "/workspaces/switch":
            self.handle_workspace_switch()
        elif self.path == "/workspaces/rename":
            self.handle_workspace_rename()
        elif self.path == "/documents/create":
            self.handle_documents_create()
        elif self.path == "/dunk-stats":
            self.handle_dunk_stats()
        elif self.path == "/briefing":
            self.handle_briefing()
        elif self.path == "/workspaces/delete":
            self.handle_workspace_delete()
        elif self.path == "/workspaces/delete/confirm":
            self.handle_workspace_delete_confirm()
        elif self.path == "/model/switch":
            self.handle_model_switch()
        else:
            self.send_error(404, "Not found")

    def handle_memory_summary(self):
        entries = load_memory()
        recent = entries[-20:]
        lines = [f"{len(entries)} total exchange(s) in memory.json (cap: {MEMORY_CAP})", ""]
        for m in reversed(recent):
            lines.append(f"[{m.get('timestamp', '?')}] ({m.get('lang', '?')})")
            lines.append(f"  Q: {m.get('question', '')}")
            lines.append(f"  A: {m.get('answer', '')}")
            lines.append("")
        body = "\n".join(lines).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b"{}"
        return json.loads(raw_body or b"{}")

    def handle_chat(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        question = (body.get("question") or "").strip()
        session_id = body.get("session_id") or str(uuid.uuid4())

        if not question:
            self.send_json(400, {"error": "missing 'question'"})
            return

        graph = load_graph()
        nodes = graph["nodes"]
        top_notes = score_notes(question, nodes)

        history = SESSIONS.setdefault(session_id, [])
        memory_entries = recent_memory(exclude=history)
        prompt = build_prompt(question, top_notes, history, memory_entries)
        answer = ask_claude(prompt)

        history.append({"question": question, "answer": answer})
        SESSIONS[session_id] = history[-MAX_HISTORY_TURNS:]
        append_memory(question, answer, "es")

        self.send_json(200, {
            "answer": answer,
            "nodes": [n["id"] for n in top_notes],
            "session_id": session_id,
            "lang": "es",
        })

    def handle_remember(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        raw_text = (body.get("text") or "").strip()
        if not raw_text:
            self.send_json(400, {"error": "missing 'text'"})
            return

        content = strip_remember_trigger(raw_text)
        if not content:
            self.send_json(400, {"error": "nothing to remember after 'recuerda que'"})
            return

        title = make_title(content)
        path = unique_capture_path(slugify(title))
        filename = os.path.basename(path)

        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{content}\n")

        try:
            build.build_graph(notes_dir(), workspace_root())
        except FileNotFoundError as e:
            self.send_json(500, {"error": str(e)})
            return

        active = workspaces.active_slug()
        project_name = (workspaces.get(active) or {}).get("name", active)
        graph = enrich_graph(notes_dir(), workspace_root(), project_name)
        workspaces.update_meta(active, note_count=len(
            [n for n in graph["nodes"] if not n.get("__entity") and not n.get("__core")]))

        nodes = graph["nodes"]
        label = build.title_from_filename(filename)
        new_node = next((n for n in nodes if n["label"] == label), nodes[-1])

        message = ask_claude(build_confirm_prompt(title, recent_memory()))
        append_memory(raw_text, message, "es")

        # the new note may have produced many new entity nodes + links (not
        # just this one file node) — the client reloads the whole workspace
        # graph and flies to `node` by label, rather than trying to splice
        # in a single node live like before entity extraction existed.
        self.send_json(200, {
            "ok": True,
            "node": new_node,
            "message": message,
        })

    def handle_project_update(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        raw_text = (body.get("text") or "").strip()
        match = PROJECT_UPDATE_RE.match(raw_text)
        if not match:
            self.send_json(400, {"error": "expected format: 'proyecto <nombre>: <actualización>'"})
            return

        name = match.group(1).strip()
        update_text = match.group(2).strip()
        if not name or not update_text:
            self.send_json(400, {"error": "missing project name or update text"})
            return

        project = upsert_project(name, update_text)
        message = ask_claude(build_project_confirm_prompt(project, recent_memory()))
        append_memory(raw_text, message, "es")

        self.send_json(200, {"ok": True, "project": project, "message": message})

    def handle_project_summary(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            body = {}

        question = (body.get("text") or "¿cómo van mis proyectos?").strip()
        projects = load_projects()
        message = ask_claude(build_project_summary_prompt(projects, recent_memory()))
        append_memory(question, message, "es")

        self.send_json(200, {"message": message, "projects": projects})

    def handle_classroom_assignments(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            body = {}

        question = (body.get("text") or "¿qué tareas tengo pendientes?").strip()
        memory_entries = recent_memory()

        try:
            courses = classroom.get_pending_assignments()
        except classroom.ClassroomError as e:
            message = ask_claude(build_classroom_error_prompt(str(e), memory_entries))
            append_memory(question, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return
        except Exception:
            # anything unforeseen (auth library internals, network blips,
            # a bug here) must still come back as JSON, never as the
            # server's default HTML error page
            traceback.print_exc()
            message = ask_claude(build_classroom_error_prompt(
                "an unexpected internal error occurred", memory_entries))
            append_memory(question, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        try:
            message = ask_claude(build_classroom_prompt(courses, memory_entries))
            append_memory(question, message, "es")
            self.send_json(200, {"ok": True, "message": message, "courses": courses})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal error building the response"})

    def handle_calendar_events(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            body = {}

        question = (body.get("text") or "¿qué tengo en mi calendario?").strip()
        memory_entries = recent_memory()

        try:
            days_groups = calendar_api.get_upcoming_events()
        except calendar_api.CalendarError as e:
            message = ask_claude(build_calendar_error_prompt(str(e), memory_entries))
            append_memory(question, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return
        except Exception:
            traceback.print_exc()
            message = ask_claude(build_calendar_error_prompt(
                "an unexpected internal error occurred", memory_entries))
            append_memory(question, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        try:
            message = ask_claude(build_calendar_prompt(days_groups, memory_entries))
            append_memory(question, message, "es")
            self.send_json(200, {"ok": True, "message": message, "days": days_groups})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal error building the response"})

    def handle_calendar_create(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        raw_text = (body.get("text") or "").strip()
        if not raw_text:
            self.send_json(400, {"error": "missing 'text'"})
            return

        memory_entries = recent_memory()

        try:
            extraction_raw = ask_claude(build_calendar_extract_prompt(raw_text, memory_entries))
            parsed = _extract_json_object(extraction_raw)
        except Exception:
            traceback.print_exc()
            message = ask_claude(build_calendar_error_prompt(
                "the requested date/time could not be understood", memory_entries))
            append_memory(raw_text, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        if not parsed.get("ok"):
            message = (parsed.get("clarification_question") or "").strip() or (
                "¿Podría repetir la fecha y hora, señor? No me quedó clara.")
            append_memory(raw_text, message, "es")
            self.send_json(200, {"ok": False, "message": message, "needs_clarification": True})
            return

        try:
            start = datetime.datetime.fromisoformat(parsed["start"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=calendar_api.local_tz())
            duration = int(parsed.get("duration_minutes") or calendar_api.DEFAULT_DURATION_MINUTES)
            title = (parsed.get("title") or "").strip() or "Evento"
        except Exception:
            traceback.print_exc()
            message = ask_claude(build_calendar_error_prompt(
                "the extracted date/time was malformed", memory_entries))
            append_memory(raw_text, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        try:
            event = calendar_api.create_event(title, start, duration_minutes=duration)
        except calendar_api.CalendarError as e:
            message = ask_claude(build_calendar_error_prompt(str(e), memory_entries))
            append_memory(raw_text, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return
        except Exception:
            traceback.print_exc()
            message = ask_claude(build_calendar_error_prompt(
                "an unexpected internal error occurred", memory_entries))
            append_memory(raw_text, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        try:
            message = ask_claude(build_calendar_create_confirm_prompt(event, memory_entries))
            append_memory(raw_text, message, "es")
            self.send_json(200, {"ok": True, "message": message, "event": event})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal error building the response"})

    def handle_files_search(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        query = (body.get("query") or "").strip()
        raw_text = (body.get("text") or "").strip()
        if not query and raw_text:
            query = FILES_SEARCH_STRIP_RE.sub("", raw_text, count=1).strip()
        if not query:
            self.send_json(400, {"error": "missing 'query'"})
            return

        results, truncated = search_files(query)
        message = ask_claude(build_file_search_prompt(query, results, truncated, recent_memory()))
        append_memory(raw_text or f"busca: {query}", message, "es")

        self.send_json(200, {
            "query": query,
            "results": results,
            "count": len(results),
            "truncated": truncated,
            "roots": FILES_ROOTS,
            "message": message,
        })

    def handle_files_read(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        raw_path = (body.get("path") or "").strip()
        if raw_path:
            # exact debug primitive: precise path in, precise content/error
            # out, no LLM involved — unchanged contract for programmatic use
            resolved = resolve_under_files_roots(raw_path)
            if resolved is None:
                self.send_json(403, {"error": "path is outside the allowed folders (~/Desktop, ~/Documents)"})
                return
            text, size, truncated, error = read_text_file(resolved)
            if error == "not_found":
                self.send_json(404, {"error": "file not found"})
                return
            if error == "io_error":
                self.send_json(500, {"error": "could not read file"})
                return
            if error == "binary":
                self.send_json(415, {"error": "binary file — cannot display as text"})
                return
            self.send_json(200, {"path": resolved, "size": size, "truncated": truncated, "content": text})
            return

        # voice/text flow: resolve a fuzzy filename via search, read it, and
        # summarize in character — any failure becomes one witty spoken
        # line (200 + ok:false), never a raw error status for this path
        raw_text = (body.get("text") or "").strip()
        query = (body.get("query") or "").strip() or FILES_READ_STRIP_RE.sub("", raw_text, count=1).strip()
        if not query:
            self.send_json(400, {"error": "missing 'path', 'query', or 'text'"})
            return

        memory_entries = recent_memory()
        results, _ = search_files(query, max_results=5)

        if not results:
            message = ask_claude(build_file_read_error_prompt(query, "no matching file was found", memory_entries))
            append_memory(raw_text or query, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        candidate = results[0]
        resolved = resolve_under_files_roots(candidate["path"])
        if resolved is None:
            message = ask_claude(build_file_read_error_prompt(candidate["name"], "it's outside the allowed folders", memory_entries))
            append_memory(raw_text or query, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        text, size, read_truncated, error = read_text_file(resolved)
        if error:
            reason = {
                "not_found": "it seems to have vanished since the search",
                "io_error": "a read error occurred",
                "binary": "it's a binary/non-text file and can't be read aloud",
            }.get(error, "it could not be read")
            message = ask_claude(build_file_read_error_prompt(candidate["name"], reason, memory_entries))
            append_memory(raw_text or query, message, "es")
            self.send_json(200, {"ok": False, "message": message, "path": resolved})
            return

        message = ask_claude(build_file_read_summary_prompt(candidate["name"], text, read_truncated, memory_entries))
        append_memory(raw_text or query, message, "es")

        self.send_json(200, {
            "ok": True,
            "path": resolved,
            "name": candidate["name"],
            "size": size,
            "truncated": read_truncated,
            "content": text,
            "message": message,
            "match_count": len(results),
        })

    def handle_open_app(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        raw_text = (body.get("text") or "").strip()
        query = (body.get("app") or "").strip() or OPEN_APP_STRIP_RE.sub("", raw_text, count=1).strip()
        if not query:
            self.send_json(400, {"error": "missing app name"})
            return

        memory_entries = recent_memory()
        canonical = match_allowed_app(query)  # None, or a value drawn verbatim from ALLOWED_APPS

        if canonical is None:
            message = ask_claude(build_open_app_refusal_prompt(query, memory_entries))
            append_memory(raw_text or query, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        try:
            result = subprocess.run(
                ["open", "-a", canonical],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            message = ask_claude(build_open_app_error_prompt(canonical, str(e), memory_entries))
            append_memory(raw_text or query, message, "es")
            self.send_json(200, {"ok": False, "message": message, "app": canonical})
            return

        if result.returncode != 0:
            reason = (result.stderr or "").strip() or "unknown error"
            message = ask_claude(build_open_app_error_prompt(canonical, reason, memory_entries))
            append_memory(raw_text or query, message, "es")
            self.send_json(200, {"ok": False, "message": message, "app": canonical})
            return

        message = ask_claude(build_open_app_confirm_prompt(canonical, memory_entries))
        append_memory(raw_text or query, message, "es")
        self.send_json(200, {"ok": True, "app": canonical, "message": message})

    def handle_graph_data(self, query):
        slug = (query.get("workspace") or [None])[0] or workspaces.active_slug()
        p = workspaces.paths(slug)
        if not os.path.exists(p["graph"]):
            self.send_json(404, {"error": f"no graph for workspace '{slug}'"})
            return
        with open(p["graph"], "r", encoding="utf-8") as f:
            body = f.read().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_workspaces_list(self):
        rows = []
        for w in workspaces.list_workspaces():
            meta_path = workspaces.paths(w["slug"])["meta"]
            note_count = 0
            summary = ""
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    note_count = meta.get("note_count", 0)
                    summary = meta.get("summary", "")
                except (json.JSONDecodeError, OSError):
                    pass
            rows.append({**w, "note_count": note_count, "summary": summary})
        self.send_json(200, {"workspaces": rows, "active_slug": workspaces.active_slug()})

    def handle_workspace_switch(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        name = (body.get("name") or "").strip()
        if not name:
            self.send_json(400, {"error": "missing 'name'"})
            return

        memory_entries = recent_memory()
        match = workspaces.find_by_name(name)
        if not match:
            message = ask_claude(build_workspace_error_prompt(
                f"no project matching \"{name}\" was found", memory_entries))
            append_memory(f"cambia al proyecto {name}", message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        try:
            workspaces.set_active(match["slug"])
            message = ask_claude(build_workspace_switch_prompt(match, recent_memory()))
            append_memory(f"cambia al proyecto {name}", message, "es")
            self.send_json(200, {
                "ok": True, "slug": match["slug"], "name": match["name"], "message": message,
            })
        except Exception:
            traceback.print_exc()
            message = ask_claude(build_workspace_error_prompt(
                "an unexpected internal error occurred", memory_entries))
            append_memory(f"cambia al proyecto {name}", message, "es")
            self.send_json(200, {"ok": False, "message": message})

    def handle_workspace_rename(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        slug = (body.get("slug") or "").strip()
        new_name = (body.get("name") or "").strip()
        if not slug or not new_name:
            self.send_json(400, {"error": "missing 'slug' or 'name'"})
            return

        try:
            workspaces.rename(slug, new_name)
        except workspaces.WorkspaceError as e:
            self.send_json(404, {"error": str(e)})
            return

        self.send_json(200, {"ok": True, "slug": slug, "name": new_name})

    def handle_documents_create(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        kind = (body.get("kind") or "").strip()
        brief = (body.get("brief") or "").strip()
        if kind not in documents.KIND_CONFIG:
            self.send_json(400, {"error": "invalid 'kind'"})
            return
        if not brief:
            self.send_json(400, {"error": "missing 'brief'"})
            return

        job_id = str(uuid.uuid4())
        DOCUMENT_JOBS[job_id] = {"status": "pending"}

        def worker():
            # runs on a background thread so the request above returns
            # instantly with "Lo estoy redactando..." — the actual claude -p
            # extraction + headless-Chrome PDF print can take a while
            try:
                result = documents.create(kind, brief)
                message = ask_claude(build_document_confirm_prompt(kind, result, recent_memory()))
                append_memory(f"[{kind}] {brief}", message, "es")
                DOCUMENT_JOBS[job_id] = {"status": "done", "message": message, "result": result}
            except Exception as e:
                traceback.print_exc()
                message = ask_claude(build_document_error_prompt(kind, str(e), recent_memory()))
                append_memory(f"[{kind}] {brief}", message, "es")
                DOCUMENT_JOBS[job_id] = {"status": "error", "message": message}

        threading.Thread(target=worker, daemon=True).start()

        self.send_json(200, {
            "ok": True,
            "job_id": job_id,
            "message": "Lo estoy redactando, un momento.",
        })

    def handle_documents_status(self, query):
        job_id = (query.get("job_id") or [None])[0]
        job = DOCUMENT_JOBS.get(job_id)
        if not job:
            self.send_json(404, {"error": "unknown job_id"})
            return
        self.send_json(200, job)

    def handle_dunk_stats(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            body = {}

        question = (body.get("text") or "¿cómo van los jugadores de DUNK hoy?").strip()
        memory_entries = recent_memory()

        try:
            stats = dunk_firebase.get_stats()
        except dunk_firebase.DunkFirebaseError as e:
            message = ask_claude(build_dunk_stats_error_prompt(str(e), memory_entries))
            append_memory(question, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return
        except Exception:
            traceback.print_exc()
            message = ask_claude(build_dunk_stats_error_prompt(
                "an unexpected internal error occurred", memory_entries))
            append_memory(question, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        try:
            message = ask_claude(build_dunk_stats_prompt(stats, memory_entries))
            append_memory(question, message, "es")
            self.send_json(200, {"ok": True, "message": message, "stats": stats})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal error building the response"})

    def handle_alerts_check(self):
        new_items = _new_urgent_alerts()
        if not new_items:
            self.send_json(200, {"alert": None})
            return
        try:
            message = ask_claude(build_alert_prompt(new_items))
        except Exception:
            traceback.print_exc()
            message = "Ojo, señor: " + "; ".join(new_items) + "."
        self.send_json(200, {"alert": message})

    def handle_briefing(self):
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            body = {}

        question = (body.get("text") or "buenos días").strip()
        memory_entries = recent_memory()

        try:
            courses = classroom.get_pending_assignments()
        except Exception:
            courses = []
        try:
            calendar_days = calendar_api.get_upcoming_events(days=1)
        except Exception:
            calendar_days = []
        try:
            stale_projects = _stale_or_paused_projects()
        except Exception:
            stale_projects = []

        try:
            message = ask_claude(build_briefing_prompt(courses, calendar_days, stale_projects, memory_entries))
            append_memory(question, message, "es")
            self.send_json(200, {"ok": True, "message": message})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal error building the briefing"})

    def handle_workspace_delete(self):
        global PENDING_DELETION
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        name = (body.get("name") or "").strip()
        password = (body.get("password") or "").strip()
        if not name:
            self.send_json(400, {"error": "missing 'name'"})
            return

        memory_entries = recent_memory()
        match = workspaces.find_by_name(name)
        if not match:
            message = ask_claude(build_delete_error_prompt(
                f"no project matching \"{name}\" was found", memory_entries))
            append_memory(f"elimina el proyecto {name}", message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        if password:
            self._resolve_pending_deletion(match, password, name)
            return

        # no password given inline — open (or refresh) a pending window;
        # the next thing said/typed that isn't another trigger is treated
        # as the password attempt (see handle_workspace_delete_confirm)
        PENDING_DELETION = {
            "slug": match["slug"],
            "name": match["name"],
            "expires_at": time.time() + PENDING_DELETION_TTL_SECONDS,
        }
        message = ask_claude(build_delete_needs_password_prompt(match["name"], memory_entries))
        append_memory(f"elimina el proyecto {name}", message, "es")
        self.send_json(200, {"ok": False, "needs_password": True, "message": message})

    def handle_workspace_delete_confirm(self):
        global PENDING_DELETION
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        password = (body.get("password") or "").strip()
        memory_entries = recent_memory()

        if not PENDING_DELETION or time.time() > PENDING_DELETION["expires_at"]:
            PENDING_DELETION = None
            message = ask_claude(build_delete_error_prompt(
                "no hay ninguna eliminación pendiente, o expiró", memory_entries))
            append_memory("[confirmación de borrado]", message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        pending = PENDING_DELETION
        self._resolve_pending_deletion(pending, password, "[confirmación de borrado]")

    def _resolve_pending_deletion(self, target, password, memory_question):
        """Shared by both the inline-password path and the separate
        confirm-turn path: verify, delete-or-refuse, always clear the
        pending state (right or wrong — a wrong guess must restate the
        whole deletion request, never get repeated free attempts)."""
        global PENDING_DELETION
        memory_entries = recent_memory()

        if not project_auth.verify_pin(password):
            PENDING_DELETION = None
            message = ask_claude(build_delete_wrong_password_prompt(memory_entries))
            append_memory(memory_question, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        try:
            workspaces.delete(target["slug"])
        except workspaces.WorkspaceError as e:
            PENDING_DELETION = None
            message = ask_claude(build_delete_error_prompt(str(e), memory_entries))
            append_memory(memory_question, message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        PENDING_DELETION = None
        message = ask_claude(build_delete_success_prompt(target["name"], memory_entries))
        append_memory(memory_question, message, "es")
        self.send_json(200, {"ok": True, "deleted": True, "message": message})

    def handle_model_switch(self):
        global ACTIVE_MODEL
        try:
            body = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        model = (body.get("model") or "").strip().lower()
        memory_entries = recent_memory()

        if model not in ALLOWED_MODELS:
            message = ask_claude(build_model_switch_error_prompt(
                f"\"{model}\" no es un modelo reconocido (sonnet/opus/haiku)", memory_entries))
            append_memory(f"cambia a modelo {model}", message, "es")
            self.send_json(200, {"ok": False, "message": message})
            return

        ACTIVE_MODEL = model
        message = ask_claude(build_model_switch_prompt(model, recent_memory()))
        append_memory(f"cambia a modelo {model}", message, "es")
        self.send_json(200, {"ok": True, "model": model, "message": message})

    def handle_workspace_ingest(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_json(400, {"error": "expected multipart/form-data"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            fields, files = parse_multipart_form(content_type, body)
        except Exception:
            traceback.print_exc()
            self.send_json(400, {"error": "could not parse upload"})
            return

        if not files:
            self.send_json(400, {"error": "no files received"})
            return

        try:
            # a single .zip upload gets extracted; otherwise every uploaded
            # file that ends in .md is treated as a note directly
            if len(files) == 1 and files[0][1].lower().endswith(".zip"):
                note_files = extract_zip_safely(files[0][2])
            else:
                note_files = []
                for _field, filename, content in files:
                    rel = sanitize_relative_path(filename)
                    if rel and rel.lower().endswith(".md"):
                        note_files.append((rel, content))

            if not note_files:
                self.send_json(400, {"error": "no markdown (.md) files found in the drop"})
                return

            provided_name = (fields.get("name") or "").strip()
            top_folder = common_top_folder(note_files)
            derived_name = provided_name or top_folder or default_project_name()
            source_label = top_folder or derived_name

            row = workspaces.register(derived_name, source_label=source_label)
            slug = row["slug"]
            p = workspaces.paths(slug)

            for rel, content in note_files:
                dest = os.path.join(p["notes"], rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(content)

            graph = build.build_graph(p["notes"], p["root"])
            note_count = len(graph["nodes"])
            sample = graph["nodes"][:5]
            summary = ask_claude(build_workspace_summary_prompt(row["name"], sample))

            # entity/concept breakdown + project-core node — one claude -p
            # call per note (cached per unchanged file), so a large drop
            # takes a while but never re-does work on a re-ingest
            enrich_graph(p["notes"], p["root"], row["name"])

            workspaces.update_meta(slug, note_count=note_count, summary=summary)

            self.send_json(200, {
                "ok": True,
                "slug": slug,
                "name": row["name"],
                "note_count": note_count,
                "summary": summary,
                "source_label": source_label,
            })
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal error during ingestion"})

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[server]", fmt % args)


def main():
    workspaces.ensure_migrated()
    socketserver.TCPServer.allow_reuse_address = True
    # Local runs remain localhost-only; a cloud host supplies PORT and binds
    # to all interfaces so its proxy can reach the app.
    with socketserver.TCPServer((HOST, PORT), Handler) as httpd:
        print(f"Serving {DIRECTORY} at http://{HOST}:{PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
