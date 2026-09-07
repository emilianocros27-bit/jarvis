"""Local PIN gate for destructive project actions (currently: deleting a
workspace). The PIN is hashed with PBKDF2-HMAC-SHA256 + a random per-install
salt — the plaintext PIN is never written to disk or logged anywhere.
"""
import hashlib
import json
import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PIN_CONFIG_PATH = os.path.join(BASE_DIR, "credentials", "pin.json")
PBKDF2_ITERATIONS = 260_000


def _hash(pin, salt_bytes, iterations):
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt_bytes, iterations).hex()


def set_pin(pin):
    salt_bytes = secrets.token_bytes(16)
    config = {
        "salt": salt_bytes.hex(),
        "hash": _hash(pin, salt_bytes, PBKDF2_ITERATIONS),
        "iterations": PBKDF2_ITERATIONS,
    }
    os.makedirs(os.path.dirname(PIN_CONFIG_PATH), exist_ok=True)
    tmp_path = PIN_CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, PIN_CONFIG_PATH)
    os.chmod(PIN_CONFIG_PATH, 0o600)


def is_configured():
    return os.path.exists(PIN_CONFIG_PATH)


def verify_pin(pin):
    if not is_configured() or not pin:
        return False
    with open(PIN_CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    salt_bytes = bytes.fromhex(config["salt"])
    candidate = _hash(pin, salt_bytes, config.get("iterations", PBKDF2_ITERATIONS))
    return secrets.compare_digest(candidate, config["hash"])
