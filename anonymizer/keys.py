"""Looking after the two secret keys (roadmap step 6).

Rules this module checks:
  - both keys exist only in the environment of the person running the tool, never in a file;
  - they are long, and they are two different keys;
  - a short public "key ID" lets people confirm which key was used without ever seeing the key.

It also offers a one-time hash key: made in memory for a single run and never shown or stored.
With it the subscriber IDs can never be rebuilt, and two releases cannot be linked to each other.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

HASH_KEY_ENV, DB_KEY_ENV = "ANON_HASH_KEY", "ANON_DB_KEY"
MIN_KEY_LENGTH = 32
SKIP_FOLDERS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}
MAX_FILE_BYTES = 2_000_000


def one_time_hash_key() -> bytes:
    """A hash key that lives only in this process. Nobody, including the operator, ever sees it."""
    return secrets.token_bytes(48)


def key_id(key: bytes | str) -> str:
    """A short public label for a key. It identifies the key and reveals nothing useful about it."""
    data = key if isinstance(key, bytes) else key.encode("utf-8")
    return hmac.new(data, b"anonymizer key id", hashlib.sha256).hexdigest()[:8]


def files_containing(root: Path, secret: str) -> list[str]:
    """Paths under root whose contents include the secret text. Used to catch a key saved by mistake."""
    needle = secret.encode("utf-8")
    hits = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or SKIP_FOLDERS & set(path.parts):
            continue
        try:
            if path.stat().st_size <= MAX_FILE_BYTES and needle in path.read_bytes():
                hits.append(str(path.relative_to(root)))
        except OSError:
            continue
    return hits


def audit(root: Path, environment: dict | None = None) -> list[tuple[str, bool, str]]:
    """Check the key rules. Returns (what was checked, passed, detail) for each rule."""
    env = os.environ if environment is None else environment
    hash_key, db_key = env.get(HASH_KEY_ENV, ""), env.get(DB_KEY_ENV, "")
    results = []
    for name, value in ((HASH_KEY_ENV, hash_key), (DB_KEY_ENV, db_key)):
        if not value:
            results.append((f"{name} is set", False, "not set in this terminal"))
            continue
        results.append((f"{name} is set", True, f"key ID {key_id(value)}"))
        results.append((f"{name} is at least {MIN_KEY_LENGTH} characters", len(value) >= MIN_KEY_LENGTH,
                        f"{len(value)} characters"))
        found = files_containing(root, value)
        results.append((f"{name} is not written in any file of this folder", not found,
                        "clean" if not found else "FOUND IN: " + ", ".join(found)))
    if hash_key and db_key:
        results.append(("the two keys are different", hash_key != db_key,
                        "different" if hash_key != db_key else "the same value is used for both"))
    return results
