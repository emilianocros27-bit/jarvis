#!/usr/bin/env python3
"""Scan a folder of markdown notes and build graph.json (nodes + links) for
the 3D knowledge galaxy viewer. Standard library only.

Usage: python3 build.py [notes_dir] [output_dir]
"""
import json
import os
import re
import sys

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]")
EXCERPT_LEN = 700


def strip_markdown(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[\[([^\]|#]+)(?:\|([^\]]*))?\]\]", lambda m: m.group(2) or m.group(1), text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_>#-]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


CAPTURE_PREFIX_RE = re.compile(r"^\d{8}-\d{6}-")


def title_from_filename(filename):
    name = os.path.splitext(filename)[0]
    # strip a live-capture chronological sort prefix (YYYYMMDD-HHMMSS-), if present
    name = CAPTURE_PREFIX_RE.sub("", name)
    name = name.replace("-", " ").replace("_", " ")
    return name.strip()


def find_notes(notes_dir):
    notes = []
    for root, _dirs, files in os.walk(notes_dir):
        for fn in sorted(files):
            if fn.lower().endswith(".md"):
                notes.append(os.path.join(root, fn))
    return sorted(notes)


def build_graph(notes_dir, out_dir):
    """Scan notes_dir, write out_dir/graph-data.js, and return the graph dict."""
    if not os.path.isdir(notes_dir):
        raise FileNotFoundError(f"Notes directory not found: {notes_dir}")

    paths = find_notes(notes_dir)
    if not paths:
        raise FileNotFoundError(f"No .md files found under {notes_dir}")

    raw = []
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw_text = f.read()
        filename = os.path.basename(path)
        label = title_from_filename(filename)
        group = os.path.basename(os.path.dirname(path)) or "root"
        plain = strip_markdown(raw_text)
        excerpt = plain[:EXCERPT_LEN]
        wikilinks = set(WIKILINK_RE.findall(raw_text))
        raw.append({
            "path": path,
            "filename": filename,
            "label": label,
            "group": group,
            "excerpt": excerpt,
            "full_text": plain,
            "wikilinks": wikilinks,
        })

    nodes = []
    for idx, note in enumerate(raw):
        nodes.append({
            "id": idx,
            "label": note["label"],
            "group": note["group"],
            "excerpt": note["excerpt"],
        })

    link_set = set()
    links = []
    n = len(raw)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            a, b = raw[i], raw[j]
            connected = False
            if b["label"] in a["wikilinks"]:
                connected = True
            elif re.search(r"\b" + re.escape(b["label"]) + r"\b", a["full_text"], flags=re.IGNORECASE):
                connected = True
            if connected:
                key = tuple(sorted((i, j)))
                if key not in link_set:
                    link_set.add(key)
                    links.append({"source": key[0], "target": key[1]})

    graph = {"nodes": nodes, "links": links}

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "graph.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)

    return graph


def main():
    notes_dir = sys.argv[1] if len(sys.argv) > 1 else "notes"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "viewer"
    try:
        graph = build_graph(notes_dir, out_dir)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    out_path = os.path.join(out_dir, "graph.json")
    print(f"Wrote {len(graph['nodes'])} nodes and {len(graph['links'])} links to {out_path}")


if __name__ == "__main__":
    main()
