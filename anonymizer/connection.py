"""Open database connections under one rule: encrypted, except on this machine.

- A connection to another machine must be encrypted (TLS). If the server cannot do that, the tool refuses.
- A connection to this machine (localhost) is allowed without TLS, so the tool can be tried on a laptop;
  callers print a warning. Set ANON_REQUIRE_TLS=1 to refuse those too.
"""
from __future__ import annotations

import os

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

STRICT_ENV = "ANON_REQUIRE_TLS"
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
WEAK_MODES = {None, "", "disable", "allow", "prefer"}


class InsecureConnectionError(RuntimeError):
    """The connection would not be encrypted and the rule does not allow that."""


def is_local(host: str | None) -> bool:
    host = host or os.environ.get("PGHOST", "")
    return host in LOCAL_HOSTS or host.startswith("/")          # a path means a local socket


def strict_from_environment() -> bool:
    return os.environ.get(STRICT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def tls_conninfo(dsn: str, strict: bool | None = None) -> tuple[str, bool]:
    """Return (connection string with the TLS rule applied, whether TLS is required)."""
    strict = strict_from_environment() if strict is None else strict
    params = conninfo_to_dict(dsn)
    required = strict or not is_local(params.get("host"))
    if required and params.get("sslmode") in WEAK_MODES:
        params["sslmode"] = "require"                             # never fall back to an unencrypted connection
    return make_conninfo(**params), required


def open_connection(dsn: str, *, read_only: bool = False, strict: bool | None = None,
                    autocommit: bool = False) -> psycopg.Connection:
    """Connect under the TLS rule. Use is_encrypted() afterwards to tell the user what they got."""
    conninfo, required = tls_conninfo(dsn, strict)
    options = "-c default_transaction_read_only=on" if read_only else None
    try:
        conn = psycopg.connect(conninfo, options=options, autocommit=autocommit)
    except psycopg.OperationalError as error:
        if required and "SSL" in str(error):
            raise InsecureConnectionError(
                "The database server did not accept an encrypted connection, and the tool refuses to "
                "connect without one. Switch SSL on in PostgreSQL (ssl = on, with a certificate)."
            ) from error
        raise
    if required and not is_encrypted(conn):                       # belt and braces
        conn.close()
        raise InsecureConnectionError("The connection is not encrypted; refusing to use it.")
    return conn


def is_encrypted(conn: psycopg.Connection) -> bool:
    return bool(conn.pgconn.ssl_in_use)


def describe(conn: psycopg.Connection) -> str:
    """One line for the user: where the connection goes and whether it is encrypted."""
    info = conn.info
    where = f"{info.host}:{info.port}/{info.dbname} as {info.user}"
    if is_encrypted(conn):
        return f"{where} (encrypted connection)"
    return f"{where} (WARNING: not encrypted; allowed only because it is this machine)"
