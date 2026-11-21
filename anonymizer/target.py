"""Write the anonymized table into the second database, encrypted (roadmap step 5).

What is stored:   public.<store_table>(block_no, payload)  - payload is ciphertext, a block of rows
What is read:     public.<output_table>                     - a view that decrypts for a session that set the key
What is recorded: public.release_info                       - when, which cookbook version, how many rows
                  public.access_log                         - who read the table, when, with which query (step 7)
                  public.run_log                            - the audit record of every release attempt (step 8)

Encryption uses PostgreSQL's pgcrypto (OpenPGP, AES-256) with the database key from ANON_DB_KEY.
The key is never stored in the database or in any file. A researcher sets it for their own session:

    SET anonymizer.db_key = '<the database key>';
    SELECT region, sum(kwh_day) FROM bills_anonymized GROUP BY region;

A release replaces the whole table in one transaction: readers see the old table or the new one, never half.
"""
from __future__ import annotations

import json
import os

import pandas as pd
import psycopg
from psycopg import sql

from .cookbook import Cookbook

DB_KEY_ENV = "ANON_DB_KEY"
MIN_KEY_LENGTH = 32
KEY_SETTING = "anonymizer.db_key"
WRITER, RESEARCHERS = "anon_writer", "anon_researchers"
INFO_TABLE = "release_info"
LOG_TABLE = "access_log"
RUN_TABLE = "run_log"                 # one line per release attempt; the tool can add lines, never change them
COUNTER_PREFIX = "access_count_"      # one sequence per researcher account counts its queries of the day
DAY_FACTOR = 1_000_000_000            # a counter holds:  day number * DAY_FACTOR + queries on that day
ALLOWED_TYPES = {"text", "integer", "bigint", "numeric", "date"}
CIPHER = "cipher-algo=aes256"


class LayoutError(RuntimeError):
    """The second database was set up for a different output table than the cookbook now describes."""


class DbKeyError(RuntimeError):
    """The database key is missing or too short."""


def load_db_key() -> str:
    key = os.environ.get(DB_KEY_ENV, "")
    if len(key) < MIN_KEY_LENGTH:
        raise DbKeyError(f"{DB_KEY_ENV} is not set or is shorter than {MIN_KEY_LENGTH} characters. "
                         "Create one with:  python -m anonymizer keygen")
    return key


def column_types(cookbook: Cookbook) -> list[tuple[str, str]]:
    """(output column, SQL type) in output order. Anything the cookbook does not type is text."""
    types = cookbook.second_database.get("column_types", {})
    return [(name, types.get(name, "text")) for name in cookbook.output_columns]


# The gatekeeper of the view (roadmap step 7). Every read of the view calls it once.
# It runs with the administrator's rights (SECURITY DEFINER) so that it can write the access log and move
# the counter, neither of which the researcher's own account may touch.
# session_user is the account that logged in; SET ROLE cannot change it.
KEY_FUNCTION = r"""
CREATE OR REPLACE FUNCTION public.anon_db_key() RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    k        text := current_setting({setting}, true);
    who      text := session_user;
    is_admin boolean := pg_has_role(session_user, current_user, 'MEMBER');
    counter  regclass := to_regclass(format('public.%I', {prefix} || session_user));
    today    bigint := (now() AT TIME ZONE 'UTC')::date - DATE '2000-01-01';
    expires  timestamptz;
    timeout  bigint;   -- milliseconds
    v        bigint;
    n        bigint;
    q        text;
    parts    text[];
BEGIN
    SELECT rolvaliduntil INTO expires FROM pg_roles WHERE rolname = who;
    IF expires < now() THEN       -- PostgreSQL checks this date only at login; this also stops a session left open
        RAISE EXCEPTION 'The account % expired on %. Ask the data owner to renew it.', who, to_char(expires, 'YYYY-MM-DD');
    END IF;
    IF k IS NULL OR k = '' THEN
        RAISE EXCEPTION 'No database key in this session. Run:  SET {name} = ''<key>'';';
    END IF;

    IF NOT is_admin THEN
        IF counter IS NULL THEN
            RAISE EXCEPTION 'The account % was not set up for reading. The data owner creates accounts with:  python -m anonymizer researcher add <name>', who;
        END IF;
        SELECT setting::bigint INTO timeout FROM pg_settings WHERE name = 'statement_timeout';
        IF timeout = 0 OR timeout > {seconds} * 1000 THEN
            RAISE EXCEPTION 'A query here may run for at most % seconds. Run:  SET statement_timeout = ''%s'';', {seconds}, {seconds};
        END IF;
        -- A sequence never rolls back, so a researcher cannot undo the count by rolling the query back.
        v := nextval(counter);
        IF v / {factor} < today THEN
            PERFORM setval(counter, today * {factor} + 1);
            n := 1;
        ELSE
            n := v % {factor};
        END IF;
        IF n > {limit} THEN
            RAISE EXCEPTION 'The account % reached its limit of % queries for today. The count starts again at midnight UTC.', who, {limit};
        END IF;
    END IF;

    -- The log keeps the query text without secrets: the key, every long quoted value and anything
    -- that looks like a key are replaced. Short values such as 'resident' stay readable for the auditor.
    q := replace(current_query(), k, '<key>');
    q := regexp_replace(q, '\$(\w*)\$.*?\$\1\$', '<hidden>', 'g');
    parts := string_to_array(q, '''');
    FOR i IN 2 .. coalesce(array_length(parts, 1), 0) BY 2 LOOP       -- every second piece is inside quotes
        IF length(parts[i]) >= 20 THEN parts[i] := '<hidden>'; END IF;
    END LOOP;
    q := regexp_replace(array_to_string(parts, ''''), '[A-Za-z0-9_+/=-]{{32,}}', '<hidden>', 'g');
    INSERT INTO {log} (account, at, query_no, query) VALUES (who, clock_timestamp(), n, left(q, 2000));
    RAISE LOG 'anonymizer access: account=% query_no=%', who, coalesce(n::text, 'administrator');
    RETURN k;
END $fn$"""


# ------------------------------------------------------------------ setting up (run once by an administrator)
def setup_statements(cookbook: Cookbook) -> list[sql.Composable]:
    """Everything the second database needs: storage, the decrypting view, and who may do what."""
    store = sql.Identifier("public", cookbook.second_database["store_table"])
    view = sql.Identifier("public", cookbook.output_table)
    info = sql.Identifier("public", INFO_TABLE)
    log = sql.Identifier("public", LOG_TABLE)
    runs = sql.Identifier("public", RUN_TABLE)
    limits = cookbook.second_database["researchers"]
    record = sql.SQL(", ").join(sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(t)) for n, t in column_types(cookbook))
    writer, researchers = sql.Identifier(WRITER), sql.Identifier(RESEARCHERS)
    return [
        sql.SQL("CREATE EXTENSION IF NOT EXISTS pgcrypto"),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"),
        sql.SQL("CREATE TABLE IF NOT EXISTS {} (block_no integer PRIMARY KEY, payload bytea NOT NULL)").format(store),
        sql.SQL("CREATE TABLE IF NOT EXISTS {} (released_at timestamptz NOT NULL, cookbook_version integer NOT NULL, "
                "row_count integer NOT NULL, block_count integer NOT NULL, group_count integer, "
                "smallest_group integer)").format(info),
        sql.SQL("CREATE TABLE IF NOT EXISTS {} (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                "account text NOT NULL, at timestamptz NOT NULL, query_no bigint, query text)").format(log),
        sql.SQL("CREATE TABLE IF NOT EXISTS {} (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                "at timestamptz NOT NULL DEFAULT now(), account text NOT NULL DEFAULT session_user, "
                "run_hash text NOT NULL, record jsonb NOT NULL)").format(runs),
        sql.SQL(KEY_FUNCTION).format(
            setting=sql.Literal(KEY_SETTING), name=sql.SQL(KEY_SETTING), log=log, prefix=sql.Literal(COUNTER_PREFIX),
            factor=sql.Literal(DAY_FACTOR), limit=sql.Literal(int(limits["daily_query_limit"])),
            seconds=sql.Literal(int(limits["query_seconds"]))),
        sql.SQL("DROP VIEW IF EXISTS {}").format(view),
        # The key function runs once per query (a materialized CTE), so one query is one log line and one count.
        sql.SQL("CREATE VIEW {view} AS WITH key AS MATERIALIZED (SELECT public.anon_db_key() AS k) "
                "SELECT r.* FROM key CROSS JOIN {store} s CROSS JOIN LATERAL "
                "jsonb_to_recordset(pgp_sym_decrypt(s.payload, key.k)::jsonb) AS r({record})")
        .format(view=view, store=store, record=record),
        sql.SQL("REVOKE ALL ON {}, {}, {}, {}, {} FROM PUBLIC").format(store, info, view, log, runs),
        # Append-only for the tool: it may add a run record, but not read, change or delete one,
        # and not choose the time or the account that are stored with it.
        sql.SQL("REVOKE ALL ON {} FROM {}").format(runs, writer),
        sql.SQL("GRANT INSERT (run_hash, record) ON {} TO {}").format(runs, writer),
        sql.SQL("REVOKE ALL ON FUNCTION public.anon_db_key() FROM PUBLIC"),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}, {}").format(writer, researchers),
        sql.SQL("GRANT SELECT, INSERT, DELETE ON {}, {} TO {}").format(store, info, writer),   # the writer never needs the view
        sql.SQL("GRANT SELECT ON {}, {} TO {}").format(view, info, researchers),               # researchers never see the store
        sql.SQL("GRANT EXECUTE ON FUNCTION public.anon_db_key() TO {}").format(researchers),   # ...nor the access log
    ]


# ------------------------------------------------------------------ writing a release
def _json_value(value, sql_type: str):
    if value is None or value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return None
    if sql_type in ("integer", "bigint"):
        return int(value) if str(value) != "" else None
    text = str(value)
    return text if text != "" or sql_type == "text" else None       # a blank number or date is NULL


def blocks(table: pd.DataFrame, cookbook: Cookbook) -> list[str]:
    """Split the table into JSON blocks. Each block is encrypted as one unit."""
    types = column_types(cookbook)
    size = int(cookbook.second_database.get("rows_per_block", 200))
    records = [{name: _json_value(row[name], sql_type) for name, sql_type in types}
               for row in table.to_dict(orient="records")]
    return [json.dumps(records[i:i + size], ensure_ascii=False) for i in range(0, len(records), size)]


def layout_problem(conn: psycopg.Connection, cookbook: Cookbook) -> str | None:
    """Does the view in the second database show exactly the cookbook's output columns? None when it does."""
    name = sql.Identifier("public", cookbook.output_table).as_string(conn)
    have = [row[0] for row in conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) AND attnum > 0 AND NOT attisdropped "
        "ORDER BY attnum", (name,)).fetchall()]
    if have == cookbook.output_columns:
        return None
    what = "has no view for the output table" if not have else "was set up for a different list of output columns"
    return (f"the second database {what}. The cookbook's output table changed, so update the database first:  "
            "python scripts/setup_target.py")


def write(conn: psycopg.Connection, table: pd.DataFrame, cookbook: Cookbook, db_key: str,
          groups: int | None = None, smallest_group: int | None = None) -> dict:
    """Replace the stored table with this one, in a single transaction. Returns what was written."""
    problem = layout_problem(conn, cookbook)
    if problem:
        raise LayoutError(problem)
    store = sql.Identifier("public", cookbook.second_database["store_table"])
    info = sql.Identifier("public", INFO_TABLE)
    payloads = blocks(table, cookbook)
    with conn.transaction():
        conn.execute(sql.SQL("DELETE FROM {}").format(store))
        with conn.cursor() as cur:
            cur.executemany(
                sql.SQL("INSERT INTO {} (block_no, payload) VALUES (%s, pgp_sym_encrypt(%s, %s, %s))").format(store),
                [(number, payload, db_key, CIPHER) for number, payload in enumerate(payloads, start=1)])
        conn.execute(sql.SQL("DELETE FROM {}").format(info))
        conn.execute(sql.SQL("INSERT INTO {} VALUES (now(), %s, %s, %s, %s, %s)").format(info),
                     (cookbook.version, len(table), len(payloads), groups, smallest_group))
    return {"rows": len(table), "blocks": len(payloads)}


def log_run(conn: psycopg.Connection, record: dict) -> None:
    """Store a run's audit record in the second database. The tool's account can only ever add lines."""
    with conn.transaction():
        conn.execute(sql.SQL("INSERT INTO {} (run_hash, record) VALUES (%s, %s::jsonb)").format(
            sql.Identifier("public", RUN_TABLE)), (record["hash"], json.dumps(record, ensure_ascii=False)))


def run_hashes(conn: psycopg.Connection) -> list[str]:
    """The fingerprints of all recorded runs, oldest first. For an administrator; the tool's account may not read them."""
    return [row[0] for row in conn.execute(sql.SQL("SELECT run_hash FROM {} ORDER BY id").format(
        sql.Identifier("public", RUN_TABLE))).fetchall()]


def read(conn: psycopg.Connection, cookbook: Cookbook, db_key: str) -> pd.DataFrame:
    """Read the table back the way a researcher does: set the key for the session, then select from the view."""
    conn.execute("SELECT set_config(%s, %s, false)", (KEY_SETTING, db_key))
    view = sql.Identifier("public", cookbook.output_table)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT * FROM {}").format(view))
        return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
