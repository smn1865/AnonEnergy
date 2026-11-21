"""The audit log (roadmap step 8): one line per run, added at the end and never changed.

Every line holds the fingerprint (SHA-256) of the line before it, so the lines form a chain.
Changing a line, removing one from the middle, or swapping two breaks the chain, and verify() says where.
The file is only ever opened for appending.

One thing a chain alone cannot show is that the newest lines were cut off the end. Releases are therefore
also recorded in the second database, in a table the tool's account can add to but never change
(target.log_run); compare() checks the file against it.

Records hold counts, names of columns and published labels. They never hold a raw value from the source.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

START = "0" * 64                      # what the first line points back to


class AuditError(RuntimeError):
    """The audit log is damaged, so nothing more may be added until someone has looked at it."""


def fingerprint(record: dict) -> str:
    """SHA-256 of the record's contents, without its own 'hash' field."""
    body = {key: value for key, value in record.items() if key != "hash"}
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(path: str | Path) -> list[dict]:
    """Every record in the log, oldest first. A missing file is an empty log."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise AuditError(f"{path.name} has a line that is not a record (line {error.lineno} of the damaged part)") from error


def verify(path: str | Path) -> tuple[bool, int, str]:
    """Walk the chain. Returns (intact, number of lines, what is wrong or 'chain intact')."""
    try:
        records = read(path)
    except AuditError as error:
        return False, 0, str(error)
    previous = START
    for number, record in enumerate(records, start=1):
        if record.get("line") != number:
            return False, len(records), f"line {number} is numbered {record.get('line')}: a line was removed or moved"
        if record.get("previous") != previous:
            return False, len(records), f"line {number} does not follow the line before it: a line was removed, added or changed"
        if record.get("hash") != fingerprint(record):
            return False, len(records), f"line {number} was changed after it was written"
        previous = record["hash"]
    return True, len(records), "chain intact"


def append(path: str | Path, record: dict) -> dict:
    """Add one record at the end. Returns it with its line number and fingerprints filled in."""
    path = Path(path)
    intact, count, problem = verify(path)
    if not intact:
        raise AuditError(f"{path.name} is damaged ({problem}). Keep the file as it is and start a new one.")
    records = read(path)
    entry = {**record, "line": count + 1, "previous": records[-1]["hash"] if records else START}
    entry["hash"] = fingerprint(entry)
    with open(path, "a", encoding="utf-8") as handle:                 # append mode: earlier lines are never rewritten
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def compare(path: str | Path, database_hashes: list[str]) -> tuple[bool, str]:
    """Check the file against the fingerprints the second database holds, oldest first."""
    in_file = [record["hash"] for record in read(path)]
    position = 0
    for number, wanted in enumerate(database_hashes, start=1):
        try:
            position = in_file.index(wanted, position) + 1
        except ValueError:
            return False, f"release {number} recorded in the database is missing from the file, or out of order"
    return True, f"all {len(database_hashes)} release(s) recorded in the database are in the file, in order"
