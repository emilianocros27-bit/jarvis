"""Read-only Firestore access to the DUNK game's Firebase project.

SAFETY: this module must NEVER call a Firestore write method — no .set(),
.update(), .delete(), .create(), transactions, or batches, anywhere, under
any circumstance. Only .get() / .stream() / .where() / .order_by() reads.
That is enforced by convention here (there is no runtime guard against it),
so any change to this file must preserve that invariant.
"""
import os
import time

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials", "dunk-firebase.json")

ACTIVE_WINDOW_MS = 24 * 60 * 60 * 1000
TOP_SCORES_LIMIT = 5

_app = None


class DunkFirebaseError(Exception):
    pass


def _client():
    global _app
    if _app is None:
        if not os.path.exists(CREDENTIALS_PATH):
            raise DunkFirebaseError(f"missing Firebase credentials at {CREDENTIALS_PATH}")
        cred = credentials.Certificate(CREDENTIALS_PATH)
        _app = firebase_admin.initialize_app(cred, name="dunk")
    return firestore.client(_app)


def _summarize_save(state):
    """Whatever fields a save actually has — no fixed schema assumed beyond
    what we've seen (totalCards/coins/packs/bestScore/ballSkin/owned)."""
    if not isinstance(state, dict):
        return "sin datos de progreso"
    owned = state.get("owned") if isinstance(state.get("owned"), dict) else {}
    parts = []
    if isinstance(state.get("totalCards"), (int, float)):
        parts.append(f"{state['totalCards']} cartas")
    elif owned:
        parts.append(f"{len(owned)} cartas distintas")
    if isinstance(state.get("coins"), (int, float)):
        parts.append(f"{state['coins']} monedas")
    if isinstance(state.get("packs"), (int, float)):
        parts.append(f"{state['packs']} paquetes")
    if isinstance(state.get("bestScore"), (int, float)):
        parts.append(f"mejor puntaje {state['bestScore']}")
    if state.get("ballSkin"):
        parts.append(f"balón: {state['ballSkin']}")
    return ", ".join(parts) if parts else "sin datos de progreso legibles"


def get_stats():
    """Read-only snapshot: how many player profiles were updated in the
    last 24h, each of those players' CURRENT save-state (there is no
    historical snapshot, so this is never a "progress gained" delta — just
    honestly the state as of right now), and the top 5 scores overall.
    Raises DunkFirebaseError on actual connection/auth/query failures —
    never for "zero active players", which is a legitimate real answer."""
    db = _client()
    cutoff_ms = int(time.time() * 1000) - ACTIVE_WINDOW_MS

    try:
        active_docs = list(
            db.collection("profiles")
            .where(filter=FieldFilter("updated", ">=", cutoff_ms))
            .stream()
        )
    except Exception as e:
        raise DunkFirebaseError(str(e))

    active_players = [
        {"uid": d.id, "username": (d.to_dict() or {}).get("username") or d.id}
        for d in active_docs
    ]

    saves_summary = []
    for player in active_players:
        try:
            snap = db.collection("saves").document(player["uid"]).get()
        except Exception as e:
            raise DunkFirebaseError(str(e))
        if not snap.exists:
            continue
        state = (snap.to_dict() or {}).get("state")
        saves_summary.append({
            "uid": player["uid"],
            "username": player["username"],
            "summary": _summarize_save(state),
        })

    try:
        top_docs = (
            db.collection("scores")
            .order_by("score", direction=firestore.Query.DESCENDING)
            .limit(TOP_SCORES_LIMIT)
            .stream()
        )
    except Exception as e:
        raise DunkFirebaseError(str(e))

    top_scores = [
        {
            "username": (d.to_dict() or {}).get("username") or (d.to_dict() or {}).get("uid") or d.id,
            "score": (d.to_dict() or {}).get("score", 0),
        }
        for d in top_docs
    ]

    return {
        "active_count": len(active_players),
        "saves_summary": saves_summary,
        "top_scores": top_scores,
    }
