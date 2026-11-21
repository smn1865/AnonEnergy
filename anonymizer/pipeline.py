"""The whole run in one place: anonymize, check, and only then write.

anonymize() returns the finished table or raises. release() writes it to the second database.
Nothing is written unless every check has passed, so a failed run leaves the previous release in place.
"""
from __future__ import annotations

import pandas as pd

from . import groups, prepare, transform
from .cookbook import Cookbook


class LeakError(RuntimeError):
    """A raw identifying value was found in the finished table."""

    def __init__(self, leaks: list[tuple[str, str]], report: groups.Report | None = None):
        self.columns = sorted({column for column, _ in leaks})     # names of columns only, never the values found
        self.report = report
        super().__init__(f"leak scan found raw values from: {', '.join(self.columns)}")


def anonymize(rows: pd.DataFrame, cookbook: Cookbook, hash_key: bytes) -> tuple[pd.DataFrame, groups.Report]:
    """Steps 2 to 4. Raises groups.GateError or LeakError instead of returning an unsafe table."""
    prepared = prepare.prepare(rows, cookbook)
    output, _ = transform.transform(prepared, cookbook, hash_key)
    protected, report = groups.protect(prepared, output, cookbook)     # raises GateError
    leaks = transform.leak_scan(rows, protected, cookbook)
    if leaks:
        raise LeakError(leaks, report)
    return protected, report


def release(rows: pd.DataFrame, cookbook: Cookbook, hash_key: bytes, db_key: str, conn) -> tuple[dict, groups.Report]:
    """Steps 2 to 5. The write happens last, in one transaction, and only if anonymize() succeeded."""
    from . import target                                               # needs the database driver

    protected, report = anonymize(rows, cookbook, hash_key)
    written = target.write(conn, protected, cookbook, db_key, report.groups, report.smallest_group)
    return written, report
