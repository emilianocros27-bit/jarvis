"""Multi-project support for Jarvis — each "workspace" (user-facing:
"proyecto") is a fully isolated notes galaxy: its own notes/, graph.json,
memory.json, and projects.json (business project-tracker). Exactly one
workspace is "active" at a time; /chat, /remember, project-tracker, and the
Classroom/Calendar conversation log all read/write through whichever
workspace is active.
"""
import datetime
import json
import os
import re
import shutil

import build

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACES_DIR = os.path.join(BASE_DIR, "workspaces")
REGISTRY_PATH = os.path.join(WORKSPACES_DIR, "_registry.json")

# legacy top-level paths this module migrates away from, once, on first boot
LEGACY_NOTES_DIR = os.path.join(BASE_DIR, "notes")
LEGACY_MEMORY_PATH = os.path.join(BASE_DIR, "memory.json")
LEGACY_PROJECTS_PATH = os.path.join(BASE_DIR, "projects.json")

DEFAULT_SLUG = "default"
ACCENT_MAP = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")

# hub-view sphere positions: spread new workspaces around a large ring so
# they never overlap, independent of how many exist already
HUB_RING_RADIUS = 400


class WorkspaceError(Exception):
    pass


def _slugify(name):
    ascii_text = (name or "").translate(ACCENT_MAP)
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-").lower()
    return ascii_text[:40] or "proyecto"


def _load_registry():
    if not os.path.exists(REGISTRY_PATH):
        return {"active_slug": None, "workspaces": []}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"active_slug": None, "workspaces": []}
        data.setdefault("active_slug", None)
        data.setdefault("workspaces", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"active_slug": None, "workspaces": []}


def _save_registry(registry):
    os.makedirs(WORKSPACES_DIR, exist_ok=True)
    tmp_path = REGISTRY_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, REGISTRY_PATH)


def paths(slug):
    root = os.path.join(WORKSPACES_DIR, slug)
    notes = os.path.join(root, "notes")
    return {
        "root": root,
        "notes": notes,
        "captures": os.path.join(notes, "captures"),
        "memory": os.path.join(root, "memory.json"),
        "projects": os.path.join(root, "projects.json"),
        "graph": os.path.join(root, "graph.json"),
        "meta": os.path.join(root, "meta.json"),
    }


def list_workspaces():
    return _load_registry()["workspaces"]


def get(slug):
    for w in list_workspaces():
        if w["slug"] == slug:
            return w
    return None


def active_slug():
    registry = _load_registry()
    slug = registry.get("active_slug")
    if slug and get(slug):
        return slug
    # active workspace vanished or was never set — fall back to whatever
    # exists so the app never hard-fails on a bad/missing pointer
    workspaces = registry["workspaces"]
    return workspaces[0]["slug"] if workspaces else DEFAULT_SLUG


def set_active(slug):
    if not get(slug):
        raise WorkspaceError(f"unknown workspace: {slug}")
    registry = _load_registry()
    registry["active_slug"] = slug
    _save_registry(registry)


def find_by_name(name):
    """Fuzzy-match a spoken/typed name against existing workspace names —
    same tolerance pattern as the business project-tracker's find_project()."""
    import difflib
    target = _slugify(name)
    candidates = {w["slug"]: _slugify(w["name"]) for w in list_workspaces()}
    matches = difflib.get_close_matches(target, list(candidates.values()), n=1, cutoff=0.6)
    if not matches:
        return None
    matched_slug = next(slug for slug, norm in candidates.items() if norm == matches[0])
    return get(matched_slug)


def _next_hub_pos(existing_count):
    import math
    angle = existing_count * (2 * math.pi / 8)  # spread around a ring, 8 slots before overlap risk
    ring = HUB_RING_RADIUS * (1 + existing_count // 8)
    return {"x": ring * math.cos(angle), "y": 0, "z": ring * math.sin(angle)}


def register(name, source_label, note_count=0, summary=""):
    """Create a brand-new workspace's registry entry + directories (notes/,
    notes/captures/) and meta.json. Returns the registry row (includes slug).
    Does NOT build the graph or set active — callers do that after writing
    note files."""
    registry = _load_registry()
    existing_ids = {w["slug"] for w in registry["workspaces"]}
    base = _slugify(name)
    slug = base
    n = 2
    while slug in existing_ids:
        slug = f"{base}-{n}"
        n += 1

    p = paths(slug)
    os.makedirs(p["captures"], exist_ok=True)

    now = datetime.datetime.now().isoformat(timespec="seconds")
    hub_pos = _next_hub_pos(len(registry["workspaces"]))
    row = {
        "slug": slug,
        "name": name.strip() or slug,
        "source_label": source_label,
        "created": now,
        "hub_pos": hub_pos,
    }
    registry["workspaces"].append(row)
    _save_registry(registry)

    with open(p["meta"], "w", encoding="utf-8") as f:
        json.dump({
            "name": row["name"],
            "source_label": source_label,
            "note_count": note_count,
            "summary": summary,
            "created": now,
        }, f, ensure_ascii=False, indent=2)

    return row


def update_meta(slug, **fields):
    p = paths(slug)
    meta = {}
    if os.path.exists(p["meta"]):
        try:
            with open(p["meta"], "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            meta = {}
    meta.update(fields)
    with open(p["meta"], "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def rename(slug, new_name):
    if not get(slug):
        raise WorkspaceError(f"unknown workspace: {slug}")
    new_name = (new_name or "").strip()
    if not new_name:
        raise WorkspaceError("empty name")
    registry = _load_registry()
    for w in registry["workspaces"]:
        if w["slug"] == slug:
            w["name"] = new_name
    _save_registry(registry)
    update_meta(slug, name=new_name)


def delete(slug):
    """Permanently removes a workspace: its registry entry AND its entire
    directory (notes, memory, projects, graph — everything). If it was the
    active workspace, falls back to whatever workspace remains first."""
    if not get(slug):
        raise WorkspaceError(f"unknown workspace: {slug}")

    registry = _load_registry()
    registry["workspaces"] = [w for w in registry["workspaces"] if w["slug"] != slug]
    if registry.get("active_slug") == slug:
        registry["active_slug"] = registry["workspaces"][0]["slug"] if registry["workspaces"] else None
    _save_registry(registry)

    root = paths(slug)["root"]
    if os.path.isdir(root):
        shutil.rmtree(root)


def ensure_migrated():
    """One-time move of the original top-level notes/memory/projects into
    workspaces/default/, preserving everything, first time this runs after
    the multi-project upgrade."""
    if os.path.exists(WORKSPACES_DIR):
        return

    os.makedirs(WORKSPACES_DIR, exist_ok=True)
    p = paths(DEFAULT_SLUG)
    os.makedirs(p["root"], exist_ok=True)

    if os.path.isdir(LEGACY_NOTES_DIR):
        shutil.move(LEGACY_NOTES_DIR, p["notes"])
    else:
        os.makedirs(os.path.join(p["notes"], "captures"), exist_ok=True)
    os.makedirs(p["captures"], exist_ok=True)

    if os.path.exists(LEGACY_MEMORY_PATH):
        shutil.move(LEGACY_MEMORY_PATH, p["memory"])
    if os.path.exists(LEGACY_PROJECTS_PATH):
        shutil.move(LEGACY_PROJECTS_PATH, p["projects"])

    note_count = 0
    try:
        graph = build.build_graph(p["notes"], p["root"])
        note_count = len(graph["nodes"])
    except FileNotFoundError:
        # no notes at all yet (fresh install) — still fine, empty graph
        with open(p["graph"], "w", encoding="utf-8") as f:
            json.dump({"nodes": [], "links": []}, f)

    now = datetime.datetime.now().isoformat(timespec="seconds")
    with open(p["meta"], "w", encoding="utf-8") as f:
        json.dump({
            "name": "Default",
            "source_label": LEGACY_NOTES_DIR,
            "note_count": note_count,
            "summary": "",
            "created": now,
        }, f, ensure_ascii=False, indent=2)

    registry = {
        "active_slug": DEFAULT_SLUG,
        "workspaces": [{
            "slug": DEFAULT_SLUG,
            "name": "Default",
            "source_label": LEGACY_NOTES_DIR,
            "created": now,
            "hub_pos": {"x": 0, "y": 0, "z": 0},
        }],
    }
    _save_registry(registry)
