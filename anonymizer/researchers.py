"""Researcher accounts on the second database (roadmap step 7).

One account per researcher, never a shared one. Every account:
  - belongs to the group anon_researchers, so it can read the view and nothing else;
  - stops working on its expiry date, including a session that was left open;
  - has its own counter, so it can run only so many queries a day;
  - appears with its name and the time in the access log for every query it runs.

All functions take an administrator connection to the second database, opened with autocommit.
A new account's password is made here, shown once, and only its scrambled form is sent to the server.
"""
from __future__ import annotations

import datetime as dt
import re
import secrets

import psycopg
from psycopg import sql

from .cookbook import Cookbook
from .target import COUNTER_PREFIX, DAY_FACTOR, LOG_TABLE, RESEARCHERS

PREFIX = "res_"
NAME = re.compile(r"[a-z][a-z0-9_]{1,30}")
TODAY_SQL = "((now() AT TIME ZONE 'UTC')::date - DATE '2000-01-01')"


class AccountError(RuntimeError):
    """The account cannot be created, changed or removed as asked."""


def account_name(name: str) -> str:
    """'anna' or 'res_anna' -> 'res_anna'. Only accounts with this prefix are ever touched."""
    short = name.strip().lower().removeprefix(PREFIX)
    if not NAME.fullmatch(short):
        raise AccountError(f"'{name}' is not a usable name: use 2 to 31 small letters, digits or _ , starting with a letter")
    return PREFIX + short


def _counter(account: str) -> sql.Identifier:
    return sql.Identifier("public", COUNTER_PREFIX + account)


def _exists(conn: psycopg.Connection, account: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (account,)).fetchone() is not None


def _require(conn: psycopg.Connection, account: str) -> None:
    if not _exists(conn, account):
        raise AccountError(f"there is no account {account}")


def _require_setup(conn: psycopg.Connection) -> None:
    if conn.execute("SELECT to_regclass(%s)", (f"public.{LOG_TABLE}",)).fetchone()[0] is None:
        raise AccountError(f"the database '{conn.info.dbname}' has no access log yet: "
                           "run 'python scripts/setup_target.py' (again) as administrator first")


def _expiry(days: int) -> dt.datetime:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).replace(microsecond=0)


def add(conn: psycopg.Connection, name: str, cookbook: Cookbook, days: int | None = None) -> dict:
    """Create one researcher's account. Returns the account name, its password (shown once) and its expiry."""
    limits = cookbook.second_database["researchers"]
    account = account_name(name)
    _require_setup(conn)
    if _exists(conn, account):
        raise AccountError(f"the account {account} already exists; use 'renew' to extend it or 'remove' to delete it")
    password = secrets.token_urlsafe(18)
    expires = _expiry(limits["account_days"] if days is None else days)
    algorithm = conn.execute("SHOW password_encryption").fetchone()[0]
    scrambled = conn.pgconn.encrypt_password(password.encode(), account.encode(), algorithm.encode()).decode()
    role = sql.Identifier(account)
    with conn.transaction():
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {} VALID UNTIL {} CONNECTION LIMIT {}").format(
            role, sql.Literal(scrambled), sql.Identifier(RESEARCHERS), sql.Literal(expires.isoformat()),
            sql.Literal(int(limits["connections"]))))
        conn.execute(sql.SQL("ALTER ROLE {} IN DATABASE {} SET statement_timeout = {}").format(
            role, sql.Identifier(conn.info.dbname), sql.Literal(f"{int(limits['query_seconds'])}s")))
        conn.execute(sql.SQL("CREATE SEQUENCE IF NOT EXISTS {} MINVALUE 1 START 1").format(_counter(account)))
        conn.execute(sql.SQL("REVOKE ALL ON SEQUENCE {} FROM PUBLIC").format(_counter(account)))
    return {"account": account, "password": password, "expires": expires, "database": conn.info.dbname,
            "host": conn.info.host, "port": conn.info.port, "daily_query_limit": limits["daily_query_limit"]}


def set_expiry(conn: psycopg.Connection, name: str, when: dt.datetime) -> dt.datetime:
    account = account_name(name)
    _require(conn, account)
    conn.execute(sql.SQL("ALTER ROLE {} VALID UNTIL {}").format(sql.Identifier(account), sql.Literal(when.isoformat())))
    return when


def renew(conn: psycopg.Connection, name: str, cookbook: Cookbook, days: int | None = None) -> dt.datetime:
    """Move the expiry date to this many days from now. The password stays the same."""
    limits = cookbook.second_database["researchers"]
    return set_expiry(conn, name, _expiry(limits["account_days"] if days is None else days))


def remove(conn: psycopg.Connection, name: str) -> dict:
    """Delete the account in one step: no new logins, open sessions closed, the account gone. Its log lines stay."""
    account = account_name(name)
    _require(conn, account)
    role = sql.Identifier(account)
    conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(role))
    closed = conn.execute("SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = %s",
                          (account,)).fetchone()[0]
    with conn.transaction():
        conn.execute(sql.SQL("DROP SEQUENCE IF EXISTS {}").format(_counter(account)))
        conn.execute(sql.SQL("DROP ROLE {}").format(role))
    return {"account": account, "sessions_closed": closed}


def _has_counter(conn: psycopg.Connection, account: str) -> bool:
    name = sql.Identifier("public", COUNTER_PREFIX + account).as_string(conn)
    return conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is not None


def unchecked_logins(conn: psycopg.Connection) -> dict | None:
    """Server rules that let someone log in without any password (method 'trust' in pg_hba.conf).

    Where such a rule applies, an account's password and expiry date protect nothing.
    Returns the file, the rules and the method to use instead; None when the server will not say.
    """
    try:
        rules = conn.execute("SELECT line_number, type, coalesce(address, 'this machine'), auth_method "
                             "FROM pg_hba_file_rules WHERE auth_method = 'trust' ORDER BY line_number").fetchall()
        return {"file": conn.execute("SHOW hba_file").fetchone()[0], "rules": rules,
                "use_instead": conn.execute("SHOW password_encryption").fetchone()[0]}
    except psycopg.Error:
        return None


def unchecked_logins_text(found: dict | None) -> list[str]:
    """The lines to show a person when the server does not check passwords."""
    if found is None:
        return ["This server accepts logins without checking the password. Look for the method 'trust' in its pg_hba.conf."]
    if not found["rules"]:
        return []
    where = ", ".join(f"line {line} ({kind} {address})" for line, kind, address, _ in found["rules"])
    return ["This PostgreSQL server lets some connections in without checking any password.",
            f"File:  {found['file']}",
            f"Rules with the method 'trust':  {where}",
            f"Fix:   change 'trust' to '{found['use_instead']}' on those lines, then reload the server. "
            "Steps are in README.md, under 'If the server does not check passwords'."]


def queries_today(conn: psycopg.Connection, name: str) -> int:
    """How many queries the counter holds for today, including ones that failed or were rolled back."""
    account = account_name(name)
    if not _has_counter(conn, account):
        return 0
    value, called, today = conn.execute(
        sql.SQL("SELECT last_value, is_called, " + TODAY_SQL + " FROM {}").format(_counter(account))).fetchone()
    return int(value % DAY_FACTOR) if called and value // DAY_FACTOR == today else 0


def accounts(conn: psycopg.Connection) -> list[dict]:
    """Every researcher account: expiry, queries today (counted and logged) and when it last read the table."""
    _require_setup(conn)
    log = sql.Identifier("public", LOG_TABLE)
    rows = conn.execute(sql.SQL(
        "SELECT r.rolname, r.rolvaliduntil, r.rolvaliduntil < now(), "
        "       (SELECT count(*) FROM {log} l WHERE l.account = r.rolname AND l.at >= (now() AT TIME ZONE 'UTC')::date::timestamp AT TIME ZONE 'UTC'), "
        "       (SELECT max(l.at) FROM {log} l WHERE l.account = r.rolname) "
        "FROM pg_roles r JOIN pg_auth_members m ON m.member = r.oid JOIN pg_roles g ON g.oid = m.roleid "
        "WHERE g.rolname = %s AND r.rolcanlogin ORDER BY r.rolname").format(log=log), (RESEARCHERS,)).fetchall()
    return [{"account": name, "expires": expires, "expired": bool(expired), "made_by_tool": _has_counter(conn, name),
             "queries_today": queries_today(conn, name) if name.startswith(PREFIX) else 0,
             "logged_today": logged, "last_read": last} for name, expires, expired, logged, last in rows]


def log(conn: psycopg.Connection, name: str | None = None, last: int = 20) -> list[tuple]:
    """The newest lines of the access log: (time, account, number of the query that day, query text)."""
    _require_setup(conn)
    table = sql.Identifier("public", LOG_TABLE)
    if name is None:
        query, values = sql.SQL("SELECT at, account, query_no, query FROM {} ORDER BY id DESC LIMIT %s").format(table), (last,)
    else:
        query = sql.SQL("SELECT at, account, query_no, query FROM {} WHERE account = %s ORDER BY id DESC LIMIT %s").format(table)
        values = (account_name(name), last)
    return conn.execute(query, values).fetchall()
