"""Local demonstration orchestration; cryptography stays in anonymizer.export."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from anonymizer import export, groups, pipeline, prepare, transform
from demo_policy import load_preview_policy

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "data" / "AnonEnergy_41_Column_Customers_Realistic.csv"
SAMPLE_LABEL = "Built-in customer sample"
DEMO_PASSPHRASE = "anon-energy-sample"
MAX_ROWS = 5_000


class DemoError(ValueError):
    """A readable error suitable for the demonstration interface."""


def read_source(data: bytes) -> pd.DataFrame:
    book = load_preview_policy()
    if len(data) > 10_000_000:
        raise DemoError("Use a CSV smaller than 10 MB for this local demonstration.")
    try:
        rows = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except (ValueError, UnicodeError, pd.errors.ParserError) as error:
        raise DemoError("This file could not be read as a UTF-8 CSV. Try the supplied sample.") from error
    missing = sorted(set(book.source_columns) - set(rows.columns))
    if missing:
        raise DemoError("The billing CSV is missing these columns: " + ", ".join(missing))
    if rows.empty:
        raise DemoError("The CSV has no bills. Load the sample or choose a populated CSV.")
    if len(rows) > MAX_ROWS:
        raise DemoError(f"Use at most {MAX_ROWS:,} bills in this demonstration.")
    rows = rows[book.source_columns].reset_index(drop=True)
    for column in ("subscriber_card_no", "bill_id"):
        if rows[column].str.strip().eq("").any():
            raise DemoError(f"Every bill needs a nonempty {column}.")
    if rows.bill_id.duplicated().any():
        raise DemoError("Bill IDs must be unique. The CSV contains repeated bill IDs.")
    if not rows.billing_month.str.fullmatch(r"\d{4}-(0[1-9]|1[0-2])").all():
        raise DemoError("Use billing months in YYYY-MM format, for example 2026-06.")
    for column in ("kwh_day", "kwh_night"):
        values = pd.to_numeric(rows[column], errors="coerce")
        if not (np.isfinite(values) & values.ge(0) & values.le(1_000_000_000)).all():
            raise DemoError(f"{column} must contain finite, nonnegative numbers up to 1 billion.")
    prepared = prepare.prepare(rows, book)
    stable = ["_location_key", "_person_key", "consumer_type", "voltage_kv"]
    if prepared.groupby("subscriber_card_no")[stable].nunique().gt(1).any().any():
        raise DemoError("An account changes identity, location, type or voltage between bills. Use consistent account metadata for this demo.")
    return rows


def tamper_with(file_bytes: bytes) -> bytes:
    """Change an actual ciphertext byte while keeping a valid envelope."""
    envelope = json.loads(file_bytes)
    payload = bytearray(base64.b64decode(envelope["ciphertext"]))
    payload[len(payload) // 2] ^= 1
    envelope["ciphertext"] = base64.b64encode(payload).decode("ascii")
    return json.dumps(envelope).encode("utf-8")


def open_package(blob: bytes, private_pem: bytes, passphrase: str):
    if len(blob) > 15_000_000:
        raise DemoError("Choose an encrypted package smaller than 15 MB.")
    try:
        return export.open_table(blob, private_pem, passphrase)
    except (export.ExportError, ValueError, KeyError, TypeError, OSError, OverflowError) as error:
        raise DemoError("Access rejected. The key or passphrase is wrong, or the package is damaged or modified.") from error


@dataclass
class DemoSession:
    hash_key: bytes = field(default_factory=lambda: secrets.token_bytes(48), repr=False)
    recipients: dict = field(default_factory=dict, repr=False)
    rows: pd.DataFrame | None = field(default=None, repr=False)
    protected: pd.DataFrame | None = field(default=None, repr=False)
    group_report: groups.Report | None = None
    source_digest: str = ""
    sealed: bytes | None = field(default=None, repr=False)
    sealed_for: str | None = None
    opened: pd.DataFrame | None = field(default=None, repr=False)
    events: list = field(default_factory=list)

    def __post_init__(self):
        if not self.recipients:
            self.recipients = {
                name: export.new_keypair(DEMO_PASSPHRASE)
                for name in ("Anna", "Boris")
            }

    def event(self, action: str, result: str):
        self.events.append({"Time (UTC)": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            "Action": action, "Result": result})

    def clear_output(self):
        self.rows = self.protected = self.group_report = None
        self.sealed = self.sealed_for = self.opened = None
        self.source_digest = ""

    def prepare(self, source: bytes):
        # A failed replacement input must never leave old output available to export.
        self.clear_output()
        rows = read_source(source)
        book = load_preview_policy()
        try:
            protected, report = pipeline.anonymize(rows, book, self.hash_key)
        except (groups.GateError, pipeline.LeakError) as error:
            self.event("Prepare dataset", "Stopped by privacy checks")
            raise DemoError(str(error)) from error
        if protected.empty:
            raise DemoError("All accounts were withheld. This batch has no releasable research rows.")
        self.rows, self.protected, self.group_report = rows, protected, report
        self.source_digest = hashlib.sha256(source).hexdigest()
        self.event("Prepare dataset", f"{len(rows)} bills → {len(protected)} research rows; selected group checks passed")

    def encrypt(self, recipient: str):
        if self.protected is None:
            raise DemoError("Prepare the research dataset first.")
        if recipient not in self.recipients:
            raise DemoError("Choose Anna or Boris.")
        book = load_preview_policy()
        self.sealed = export.seal_table(self.protected, self.recipients[recipient][1], book.version)
        self.sealed_for = recipient
        self.opened = None
        self.event("Encrypt package", f"Sealed for {recipient}; {len(self.sealed):,} bytes")
        return self.sealed

    def decrypt(self, recipient: str, passphrase: str, *, modified: bool = False):
        self.opened = None
        if self.sealed is None:
            raise DemoError("Encrypt a package in the Data Owner tab first.")
        if recipient not in self.recipients:
            raise DemoError("Choose Anna or Boris.")
        blob = tamper_with(self.sealed) if modified else self.sealed
        try:
            table, info = open_package(blob, self.recipients[recipient][0], passphrase)
        except DemoError:
            self.event("Modified package" if modified else f"Decrypt as {recipient}", "Rejected")
            raise
        self.opened = table
        self.event(f"Decrypt as {recipient}", f"Opened {len(table)} research rows; integrity verified")
        return table, info

    def checks(self) -> dict:
        if self.protected is None:
            return {}
        book = load_preview_policy()
        return {"group_gate": self.group_report.gate,
                "minimum_people_per_selected_group": self.group_report.smallest_group,
                "selected_groups": self.group_report.groups,
                "identifier_leaks_found": len(transform.leak_scan(self.rows, self.protected, book)),
                "source_rows": len(self.rows), "research_rows": len(self.protected),
                "source_columns": len(self.rows.columns), "research_columns": len(self.protected.columns),
                "places_named": self.group_report.places_shown,
                "places_before": self.group_report.places_before,
                "rows_withheld": len(self.rows) - len(self.protected),
                "scope": "Selected group-size and raw-identifier checks; consumption linkage remains possible."}

    def consumption_changes(self) -> pd.DataFrame:
        """Owner-only comparison; not included in the 14-column research export."""
        if self.rows is None or self.protected is None:
            return pd.DataFrame()
        source = self.rows[["bill_id", "kwh_day", "kwh_night"]].copy()
        source["bill_id"] = source.bill_id.map(lambda value: transform.keyed_hash(self.hash_key, "bill_id", value))
        paired = self.protected[["bill_id", "billing_month", "kwh_day", "kwh_night"]].merge(
            source, on="bill_id", how="left", suffixes=("_prepared", "_original"), validate="one_to_one")
        for column in ("kwh_day", "kwh_night"):
            before = pd.to_numeric(paired[column + "_original"])
            after = pd.to_numeric(paired[column + "_prepared"])
            paired[column + "_change"] = after - before
            paired[column + "_change_pct"] = 100 * (after - before) / before.where(before != 0)
        return paired

    def save_encrypted_copy(self, folder: Path) -> Path:
        if self.sealed is None:
            raise DemoError("Encrypt a package first.")
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{self.sealed_for.lower()}-{hashlib.sha256(self.sealed).hexdigest()[:12]}.anonexport"
        destination = folder / name
        # Exclusive creation avoids overwriting any existing package.
        try:
            with destination.open("xb") as handle:
                os.chmod(destination, 0o600)
                handle.write(self.sealed)
        except FileExistsError:
            pass
        return destination
