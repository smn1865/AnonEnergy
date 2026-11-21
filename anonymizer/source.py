"""Connect to the source database and match its columns to the cookbook (roadmap step 2).

The tool only ever reads the source. The connection is opened read-only on top of
whatever the account is allowed to do, so a mistake here cannot change the data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import psycopg
from psycopg import sql

from .connection import open_connection
from .cookbook import Cookbook


def connect(dsn: str) -> psycopg.Connection:
    """Open a read-only connection under the TLS rule. Every transaction on it refuses writes."""
    return open_connection(dsn, read_only=True)


def discover_columns(conn: psycopg.Connection, schema: str, table: str) -> list[tuple[str, str]]:
    """Return (column name, data type) for the source table, in table order."""
    rows = conn.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    ).fetchall()
    if not rows:
        raise LookupError(f"table {schema}.{table} not found, or this account cannot see it")
    return [(name, dtype) for name, dtype in rows]


@dataclass
class MatchReport:
    matched: list[tuple[str, str, str]] = field(default_factory=list)   # (column, data type, action)
    unknown: list[tuple[str, str]] = field(default_factory=list)        # in the source, not in the cookbook
    missing: list[str] = field(default_factory=list)                    # in the cookbook, not in the source

    @property
    def ok(self) -> bool:
        """True when every cookbook column exists in the source."""
        return not self.missing

    def lines(self) -> list[str]:
        out = [f"matched {len(self.matched)} columns"]
        for column, dtype, action in self.matched:
            out.append(f"  {column:<22} {dtype:<28} -> {action}")
        if self.unknown:
            out.append(f"NOT IN COOKBOOK ({len(self.unknown)}): left out of the output")
            out += [f"  {column:<22} {dtype}" for column, dtype in self.unknown]
        if self.missing:
            out.append(f"MISSING FROM SOURCE ({len(self.missing)}): the run cannot continue")
            out += [f"  {column}" for column in self.missing]
        return out


def match_columns(cookbook: Cookbook, source_columns: list[tuple[str, str]]) -> MatchReport:
    """Pair every source column with its cookbook rule, by exact name."""
    report = MatchReport()
    seen = set()
    for name, dtype in source_columns:
        seen.add(name)
        rule = cookbook.rules.get(name)
        if rule is None:
            report.unknown.append((name, dtype))
        else:
            report.matched.append((name, dtype, rule.action))
    report.missing = [c for c in cookbook.source_columns if c not in seen]
    return report


def read_rows(conn: psycopg.Connection, cookbook: Cookbook, columns: list[str]) -> pd.DataFrame:
    """Read the matched columns as text, so IDs keep leading zeros and nothing is reformatted."""
    query = sql.SQL("SELECT {cols} FROM {table}").format(
        cols=sql.SQL(", ").join(
            sql.SQL("{}::text AS {}").format(sql.Identifier(c), sql.Identifier(c)) for c in columns
        ),
        table=sql.Identifier(cookbook.schema, cookbook.table),
    )
    with conn.cursor() as cur:
        cur.execute(query)
        return pd.DataFrame(cur.fetchall(), columns=columns, dtype="object")
