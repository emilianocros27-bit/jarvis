"""Shared Google OAuth plumbing for Jarvis — one client secret, one cached
token, one consent flow covering every Google integration (Classroom,
Calendar, ...). Individual integration modules import get_credentials()
from here instead of managing their own token file or scope list.
"""
import os

# Google's token endpoint sometimes returns the granted scopes reordered, or
# expanded/collapsed relative to what was requested (seen in practice: our
# 4-scope request came back as a 3-scope grant missing coursework.me.readonly
# from the returned string, though it was in fact usable). oauthlib treats
# ANY such mismatch as a hard error by default ("Scope has changed from X to
# Y") unless this is set — this is the documented, standard relaxation.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET_PATH = os.path.join(BASE_DIR, "credentials", "client_secret.json")
TOKEN_PATH = os.path.join(BASE_DIR, "token.json")

# combined scopes across every Google integration Jarvis has. Adding a new
# integration means adding its scopes here, then deleting token.json once
# so the next consent flow covers everything together.
SCOPES = [
    # Classroom — read-only, never write
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    # Calendar — full read/write (check + create events)
    "https://www.googleapis.com/auth/calendar",
]


class AuthError(Exception):
    pass


def get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception:
            creds = None  # refresh token dead — fall through to a fresh consent flow

    if not os.path.exists(CLIENT_SECRET_PATH):
        raise AuthError(f"missing OAuth client file at {CLIENT_SECRET_PATH}")

    # opens the user's browser for the standard Google consent screen and
    # blocks until they approve (or deny) — expected on first run, or
    # whenever the scope list has grown since the cached token was issued.
    # timeout_seconds is critical: our HTTP server is single-threaded, so
    # without it, an abandoned/denied consent screen wedges run_local_server()
    # forever — freezing every other Jarvis request, not just this one.
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
    try:
        creds = flow.run_local_server(port=0, timeout_seconds=180)
    except Exception as e:
        raise AuthError(f"consent flow did not complete: {e}")
    _save_token(creds)
    return creds


def _save_token(creds):
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
