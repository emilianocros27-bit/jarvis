"""Standalone documents engine for Jarvis — turns one spoken sentence into a
formatted single-page PDF (receipt, report, or letter).

Fully self-contained: its own `claude -p` call, its own HTML rendering, its
own PDF printing via headless Chrome. Callable directly with no HTTP server
running: `documents.create("receipt", "recibo de $450 de material...")`.
"""
import datetime
import html as html_mod
import json
import os
import re
import subprocess
import tempfile

OUTPUT_ROOT = os.path.expanduser("~/Desktop/jarvis-documents")
LEDGER_PATH = os.path.join(OUTPUT_ROOT, "ledger.jsonl")
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

KIND_CONFIG = {
    "receipt": {"folder": "receipts", "prefix": "RECIBO"},
    "report": {"folder": "reports", "prefix": "REPORTE"},
    "letter": {"folder": "letters", "prefix": "CARTA"},
}


class DocumentsError(Exception):
    pass


def _ask_claude(prompt):
    try:
        result = subprocess.run(
            ["claude", "-p", "--safe-mode", prompt],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise DocumentsError("the `claude` CLI was not found on PATH")
    except subprocess.TimeoutExpired:
        raise DocumentsError("claude took too long to respond")
    if result.returncode != 0:
        raise DocumentsError((result.stderr or "unknown error from claude").strip())
    return result.stdout.strip()


def _extract_json_object(text):
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise DocumentsError("no JSON object found in the model's output")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise DocumentsError(f"malformed JSON from the model: {e}")


def _coerce_amount(value):
    """Never trust the model's arithmetic — this just turns whatever it
    wrote ("$450", "450.00", 450) into a plain float for OUR math."""
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.\-]", "", str(value or ""))
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


def _esc(value):
    return html_mod.escape(str(value if value is not None else ""))


PROMPTS = {
    "receipt": (
        "Extract a receipt/expense from this Spanish sentence: \"{brief}\". "
        "Respond with ONLY raw JSON — no markdown fences, no extra text — "
        "shaped exactly like:\n"
        '{{"title": "<short title>", '
        '"items": [{{"description": "<item>", "amount": <plain number, no currency symbols>}}], '
        '"category": "<one or two words>", '
        '"date": "<YYYY-MM-DD; today if not stated>", '
        '"notes": "<short note, or empty string>"}}\n'
        "If only one expense is mentioned, \"items\" is a single-element "
        "array. If several are mentioned, include one element per item — "
        "never compute a total yourself, just list the items."
    ),
    "report": (
        "Extract a formal report outline from this Spanish sentence: "
        "\"{brief}\". Respond with ONLY raw JSON — no markdown fences, no "
        "extra text — shaped exactly like:\n"
        '{{"title": "<short title>", '
        '"subject": "<one sentence summarizing the report>", '
        '"sections": [{{"heading": "<section heading>", "body": "<2-4 sentences, Spanish>"}}], '
        '"date": "<YYYY-MM-DD; today if not stated>", '
        '"author": "<a plausible author name/role if implied, else empty string>"}}\n'
        "Produce 2-4 sections that genuinely make sense for the stated subject."
    ),
    "letter": (
        "Extract a simple formal letter from this Spanish sentence: "
        "\"{brief}\". Respond with ONLY raw JSON — no markdown fences, no "
        "extra text — shaped exactly like:\n"
        '{{"title": "<short subject line>", '
        '"recipient": "<name or role mentioned, else \\"Señor/a\\">", '
        '"body_paragraphs": ["<paragraph 1, Spanish, formal register>", "..."], '
        '"date": "<YYYY-MM-DD; today if not stated>", '
        '"sender": "<a plausible sender name if implied, else empty string>"}}\n'
        "Produce 2-3 short, formal paragraphs that actually address what "
        "was asked."
    ),
}


COMMON_STYLE = """
<style>
  @page { size: letter; margin: 0; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 60px 66px;
    background: #F7F3EA;
    color: #16130D;
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 14px;
    line-height: 1.65;
  }
  .kind-label {
    font-variant: small-caps;
    letter-spacing: 0.18em;
    font-size: 12px;
    color: #8a806a;
    margin: 0 0 6px 0;
  }
  .headline {
    font-size: 27px;
    font-weight: 700;
    margin: 0 0 18px 0;
    letter-spacing: 0.01em;
  }
  .meta-row {
    display: flex;
    justify-content: space-between;
    font-size: 12.5px;
    color: #5c5442;
    margin-bottom: 26px;
  }
  hr.rule {
    border: none;
    border-top: 1px solid rgba(22,19,13,0.18);
    margin: 22px 0;
  }
</style>
"""


def _render_receipt(data):
    title = data.get("title") or "Recibo"
    items_raw = data.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        # tolerate the flatter single-field shape too, just in case
        items_raw = [{
            "description": data.get("item_description") or data.get("description") or "Artículo",
            "amount": data.get("amount", 0),
        }]

    items = []
    total = 0.0
    for raw_item in items_raw:
        amount = _coerce_amount(raw_item.get("amount"))
        total += amount
        items.append({"description": raw_item.get("description") or "Artículo", "amount": amount})

    category = data.get("category") or ""
    date = data.get("date") or datetime.date.today().isoformat()
    notes = data.get("notes") or ""

    rows = "".join(
        f'<div class="item-row"><span>{_esc(it["description"])}</span>'
        f'<span>${it["amount"]:,.2f}</span></div>'
        for it in items
    )

    doc = f"""<!doctype html><html><head><meta charset="utf-8">{COMMON_STYLE}
<style>
  .item-row {{ display:flex; justify-content:space-between; padding:8px 0;
    border-bottom:1px solid rgba(22,19,13,0.08); font-size:14px; }}
  .total-box {{ margin-top:24px; padding:18px 22px; background:rgba(22,19,13,0.05);
    border:1px solid rgba(22,19,13,0.15); border-radius:6px;
    display:flex; justify-content:space-between; align-items:baseline; }}
  .total-label {{ font-variant:small-caps; letter-spacing:0.1em; font-size:13px; color:#5c5442; }}
  .total-amount {{ font-size:30px; font-weight:700; }}
  .notes {{ margin-top:26px; font-size:12.5px; font-style:italic; color:#5c5442; }}
</style>
</head><body>
  <div class="kind-label">Recibo</div>
  <div class="headline">{_esc(title)}</div>
  <div class="meta-row"><span>Fecha: {_esc(date)}</span><span>{_esc(category)}</span></div>
  <hr class="rule">
  {rows}
  <div class="total-box"><span class="total-label">Total</span><span class="total-amount">${total:,.2f}</span></div>
  {f'<div class="notes">{_esc(notes)}</div>' if notes else ''}
</body></html>"""
    return doc, {"title": title, "total": round(total, 2)}


def _render_report(data):
    title = data.get("title") or "Reporte"
    subject = data.get("subject") or ""
    sections = data.get("sections") or []
    date = data.get("date") or datetime.date.today().isoformat()
    author = data.get("author") or ""

    sections_html = "".join(
        f'<div class="section"><div class="section-heading">{_esc(s.get("heading"))}</div>'
        f'<div class="section-body">{_esc(s.get("body"))}</div></div>'
        for s in sections
    )

    doc = f"""<!doctype html><html><head><meta charset="utf-8">{COMMON_STYLE}
<style>
  .subject {{ font-style:italic; color:#5c5442; margin-bottom:22px; }}
  .section {{ margin-bottom:20px; }}
  .section-heading {{ font-variant:small-caps; letter-spacing:0.1em; font-weight:700;
    font-size:14px; margin-bottom:6px; }}
  .section-body {{ font-size:13.5px; }}
</style>
</head><body>
  <div class="kind-label">Reporte</div>
  <div class="headline">{_esc(title)}</div>
  <div class="meta-row"><span>Fecha: {_esc(date)}</span><span>{_esc(author)}</span></div>
  {f'<div class="subject">{_esc(subject)}</div>' if subject else ''}
  <hr class="rule">
  {sections_html}
</body></html>"""
    return doc, {"title": title}


def _render_letter(data):
    title = data.get("title") or "Carta"
    recipient = data.get("recipient") or "Señor/a"
    paragraphs = data.get("body_paragraphs") or []
    date = data.get("date") or datetime.date.today().isoformat()
    sender = data.get("sender") or ""

    paragraphs_html = "".join(f"<p>{_esc(p)}</p>" for p in paragraphs)

    doc = f"""<!doctype html><html><head><meta charset="utf-8">{COMMON_STYLE}
<style>
  .letter-date {{ text-align:right; font-size:12.5px; color:#5c5442; margin-bottom:40px; }}
  .salutation {{ margin-bottom:18px; }}
  .letter-body p {{ margin:0 0 14px 0; font-size:14px; }}
  .closing {{ margin-top:44px; }}
  .sender-name {{ margin-top:46px; font-weight:700; }}
</style>
</head><body>
  <div class="kind-label">Carta</div>
  <div class="headline">{_esc(title)}</div>
  <div class="letter-date">{_esc(date)}</div>
  <div class="salutation">Estimado/a {_esc(recipient)}:</div>
  <div class="letter-body">{paragraphs_html}</div>
  <div class="closing">Atentamente,</div>
  <div class="sender-name">{_esc(sender)}</div>
</body></html>"""
    return doc, {"title": title, "recipient": recipient}


RENDERERS = {
    "receipt": _render_receipt,
    "report": _render_report,
    "letter": _render_letter,
}


def _next_number(kind):
    if not os.path.exists(LEDGER_PATH):
        return 1
    count = 0
    with open(LEDGER_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("kind") == kind:
                count += 1
    return count + 1


def _append_ledger(entry):
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _print_pdf(html_path, pdf_path):
    if not os.path.exists(CHROME_PATH):
        raise DocumentsError(f"Chrome not found at {CHROME_PATH}")
    try:
        result = subprocess.run(
            [
                CHROME_PATH,
                "--headless",
                f"--print-to-pdf={pdf_path}",
                "--no-pdf-header-footer",
                f"file://{html_path}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise DocumentsError("Chrome timed out generating the PDF")
    if result.returncode != 0 or not os.path.exists(pdf_path):
        raise DocumentsError((result.stderr or "Chrome failed to print the PDF").strip())


def create(kind, brief):
    """Turns one spoken/typed sentence into a saved PDF. Returns
    {ok, kind, filename, folder, path, number, title, ...kind-specific meta}.
    Raises DocumentsError on any failure — never partially writes a ledger
    entry for a document that didn't actually get saved."""
    if kind not in KIND_CONFIG:
        raise DocumentsError(f"unknown document kind: {kind}")
    brief = (brief or "").strip()
    if not brief:
        raise DocumentsError("empty brief")

    prompt = PROMPTS[kind].format(brief=brief.replace('"', "'"))
    data = _extract_json_object(_ask_claude(prompt))
    html_doc, meta = RENDERERS[kind](data)

    cfg = KIND_CONFIG[kind]
    number = _next_number(kind)
    year = datetime.datetime.now().year
    filename = f"{cfg['prefix']}-{year}-{number:03d}.pdf"
    folder_path = os.path.join(OUTPUT_ROOT, cfg["folder"])
    os.makedirs(folder_path, exist_ok=True)
    pdf_path = os.path.join(folder_path, filename)

    with tempfile.TemporaryDirectory() as tmp_dir:
        html_path = os.path.join(tmp_dir, "doc.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
        _print_pdf(html_path, pdf_path)

    _append_ledger({
        "kind": kind,
        "number": number,
        "filename": filename,
        "folder": cfg["folder"],
        "brief": brief,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        **meta,
    })

    return {
        "ok": True,
        "kind": kind,
        "filename": filename,
        "folder": cfg["folder"],
        "path": pdf_path,
        "number": number,
        **meta,
    }
