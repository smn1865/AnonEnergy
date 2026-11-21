"""Command line for steps 1 to 8.

    python -m anonymizer check        validate the cookbook and print the output table
    python -m anonymizer discover     connect to the source and match its columns to the cookbook
    python -m anonymizer prepare      also clean locations and mark people; prints a summary
    python -m anonymizer keygen       print a new random hash key and how to set it
    python -m anonymizer transform    apply the column rules and show the result (step 3 only)
    python -m anonymizer protect      steps 2 to 4 in one go: the anonymized table, or nothing if the gate fails
    python -m anonymizer release      steps 2 to 5: write the anonymized table, encrypted, into the second database
    python -m anonymizer keys         check that both keys are set, long, different, and not saved in any file here

End-to-end encrypted exports, for one researcher at a time:
    python -m anonymizer researcher-keygen --name anna     the researcher makes a key pair on their own machine
    python -m anonymizer export --to anna_public.pem --out anna.anonexport      the tool seals a release to it
    python -m anonymizer open-export anna.anonexport --key anna_private.pem --out data.csv    only anna can do this

Every protect, release and export run adds a line to the audit log and writes the one-screen report:
    python -m anonymizer report       show the report of the latest run again and re-create report.html
    python -m anonymizer audit        list the runs in the audit log and check that no line was changed or removed
                                      (add --db to compare it with the run records kept in the second database)

Researcher accounts on the second database (run by the database administrator):
    python -m anonymizer researcher add anna        create her account; prints its password once
    python -m anonymizer researcher list            every account: expiry, queries today, last read
    python -m anonymizer researcher log             the newest lines of the access log (add a name for one account)
    python -m anonymizer researcher renew anna      move her expiry date forward
    python -m anonymizer researcher remove anna     close her sessions and delete the account, in one step

prepare, transform, protect, release and export read the source database, or the mock file with:  --from-csv data/mock_ena_bills.csv
protect, release and export accept --one-time-hash-key: a hash key made in memory for this run and never shown.
transform and protect can save what they produced with:  --out anonymized.csv

Settings come from the environment:
    SOURCE_DSN      connection string for the read-only source account
    ANON_HASH_KEY   secret key for hashing (transform and protect); never put it in a file
    TARGET_DSN      connection string for the tool's account on the second database (release only)
    ANON_DB_KEY     secret key that encrypts the second database (release only); never put it in a file
    ADMIN_DSN       administrator connection string (researcher commands); without it the password is asked for
    TARGET_DB       name of the second database for the researcher commands (default: anonymized)
    ANON_REQUIRE_TLS  set to 1 to refuse unencrypted connections even on this machine
    COOKBOOK        path to the cookbook (default: cookbook.yaml)
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

import pandas as pd

from . import cookbook as cookbook_module
from . import groups as groups_module
from . import keys as keys_module
from . import pipeline as pipeline_module
from . import prepare as prepare_module
from . import transform as transform_module


def _load():
    return cookbook_module.load(os.environ.get("COOKBOOK", "cookbook.yaml"))


def _connect_and_match(book):
    from . import source                           # needs the database driver; the CSV path does not

    dsn = os.environ.get("SOURCE_DSN")
    if not dsn:
        sys.exit("SOURCE_DSN is not set (connection string for the read-only source account).\n"
                 "To try things on the mock file instead, add:  --from-csv data/mock_ena_bills.csv")
    conn = source.connect(dsn)
    report = source.match_columns(book, source.discover_columns(conn, book.schema, book.table))
    return source, conn, report


def _read_rows(book, from_csv: str | None) -> pd.DataFrame:
    """Rows from the mock file, or from the source database through the read-only account."""
    if from_csv:
        rows = pd.read_csv(from_csv, dtype=str, keep_default_na=False)
        missing = [c for c in book.source_columns if c not in rows.columns]
        if missing:
            sys.exit(f"{from_csv} is missing columns the cookbook names: {', '.join(missing)}")
        return rows[[c for c in rows.columns if c in book.rules]]
    source, conn, report = _connect_and_match(book)
    if not report.ok:
        sys.exit("\n".join(report.lines()))
    rows = source.read_rows(conn, book, [name for name, _, _ in report.matched])
    conn.close()
    return rows


def cmd_check(_args) -> int:
    book = _load()
    listed = sum(1 for r in book.rules.values() if r.listed)
    print(f"cookbook version {book.version}: {len(book.rules)} source columns ({listed} on the agreed list)")
    for action in sorted({r.action for r in book.rules.values()}):
        print(f"  {action:<18} {', '.join(book.columns_with(action))}")
    print(f"output table '{book.output_table}' ({len(book.output_columns)} columns):")
    print("  " + ", ".join(book.output_columns))
    return 0


def cmd_discover(_args) -> int:
    book = _load()
    _, conn, report = _connect_and_match(book)
    conn.close()
    print("\n".join(report.lines()))
    return 0 if report.ok else 1


def cmd_prepare(args) -> int:
    book = _load()
    rows = _read_rows(book, args.from_csv)
    prepared = prepare_module.prepare(rows, book)
    summary = prepare_module.location_summary(prepared)
    minimum = book.parameters["min_group_size"]
    small = summary[summary.people < minimum]
    print(f"{len(rows)} rows, {prepared.subscriber_card_no.nunique()} cards, "
          f"{prepared._person_key.nunique()} people or companies, {len(summary)} places")
    print(f"{len(small)} places have fewer than {minimum} people and will need step 4:")
    print(summary.rename(columns={"_region": "region", "_community": "community", "_settlement": "settlement"})
          [["region", "community", "settlement", "cards", "people", "rows"]].to_string(index=False))
    return 0


def cmd_keygen(_args) -> int:
    hash_key, db_key = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    print("Two new keys (shown once, not saved anywhere). Set both in the terminal you run the tool from.\n")
    print("PowerShell:")
    print(f'  $env:ANON_HASH_KEY = "{hash_key}"')
    print(f'  $env:ANON_DB_KEY = "{db_key}"')
    print("cmd:")
    print(f"  set ANON_HASH_KEY={hash_key}")
    print(f"  set ANON_DB_KEY={db_key}")
    print("macOS/Linux:")
    print(f'  export ANON_HASH_KEY="{hash_key}"')
    print(f'  export ANON_DB_KEY="{db_key}"')
    print("\nANON_HASH_KEY makes the subscriber IDs and the noise. It is one-way and is never shared.")
    print("  Same key = same IDs in every run. Throw it away and the IDs can never be rebuilt.")
    print("ANON_DB_KEY encrypts the second database. Researchers need it to read; the tool needs it to write.")
    return 0


def cmd_transform(args) -> int:
    book = _load()
    try:
        key = transform_module.load_key()
    except transform_module.HashKeyError as error:
        sys.exit(str(error))
    rows = _read_rows(book, args.from_csv)
    prepared = prepare_module.prepare(rows, book)
    output, report = transform_module.transform(prepared, book, key)
    leaks = transform_module.leak_scan(rows, output, book)

    print(f"{report['rows']} rows in, {len(output)} rows out")
    print(f"columns: {len(rows.columns)} in the source, {len(output.columns)} in the output, "
          f"{len(report['deleted_columns'])} deleted")
    print(f"hashed with the secret key: {', '.join(report['hashed_columns'])}")
    if report["kwh_noise_percent"]:
        print(f"kWh noise: every value moved by up to +/- {report['kwh_noise_percent']:g}%, fixed by the secret key")
    for column, info in report["kwh_caps"].items():
        print(f"{column}: rounded to the nearest {book.parameters['kwh_round_to']}; "
              f"household cap {info['cap']} kWh applied to {info['rows_capped']} rows")
    print(f"tariff: {report.get('tariff_category_rows_merged', 0)} rows merged into the residential tiers")
    print(f"substation: {report.get('substation_id_values_blanked', 0)} substations serving fewer than "
          f"{book.parameters['min_group_size']} people were blanked "
          f"({report.get('substation_id_rows_blanked', 0)} rows)")
    print(f"leak scan: {len(leaks)} raw identifying values found in the output"
          + ("" if not leaks else " -> " + ", ".join(sorted({c for c, _ in leaks}))))
    print("note: small settlements are still shown as they are; step 4 merges them.")

    print("\nfirst rows of the output:")
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(output.head(args.rows).to_string(index=False))
    if args.out:
        output.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\nsaved {len(output)} rows to {args.out} (a preview: not yet protected against small groups)")
    return 1 if leaks else 0


def _hash_key(args) -> bytes:
    """The hash key for this run: from the environment, or a one-time key that nobody ever sees."""
    if getattr(args, "one_time_hash_key", False):
        print("hash key: one-time key, made for this run and not kept (IDs cannot be rebuilt or linked to other runs)")
        return keys_module.one_time_hash_key()
    try:
        key = transform_module.load_key()
    except transform_module.HashKeyError as error:
        sys.exit(str(error))
    print(f"hash key: from {transform_module.KEY_ENV}, key ID {keys_module.key_id(key)}")
    return key


def _stopped(error: Exception, previous: bool = False) -> int:
    print(f"STOPPED: {error}")
    print("Nothing was written." + (" The second database still holds the previous release." if previous else ""))
    return 2


def _cookbook_path() -> str:
    return os.environ.get("COOKBOOK", "cookbook.yaml")


def _audit_ready(book) -> None:
    """Stop before any work if the audit log is damaged: a run that cannot be recorded must not happen."""
    from . import audit

    intact, _, problem = audit.verify(book.risk_report["audit_log"])
    if not intact:
        sys.exit(f"STOPPED: the audit log {book.risk_report['audit_log']} is damaged ({problem}).\n"
                 "Keep that file as evidence, move it away, and run again to start a new log.")


def _source_label(from_csv: str | None) -> str:
    if from_csv:
        return f"file {Path(from_csv).name}"
    from psycopg.conninfo import conninfo_to_dict

    parts = conninfo_to_dict(os.environ.get("SOURCE_DSN", ""))       # the password is never copied
    return f"database {parts.get('dbname', '?')} on {parts.get('host', 'localhost')} as {parts.get('user', '?')}"


def _key_ids(args, key: bytes, **more) -> dict:
    hash_id = "one-time, not kept" if getattr(args, "one_time_hash_key", False) else keys_module.key_id(key)
    return {"hash_key": hash_id, **more}


def _record_run(command: str, book, rows, *, source: str, destination: str, keys: dict, conn=None, **result) -> dict:
    """Describe the run, add it to the audit log, write the one-screen report and print the summary."""
    from . import audit
    from . import report as report_module

    settings = book.risk_report
    record = report_module.build_record(command, rows, book, _cookbook_path(), source=source,
                                        destination=destination, keys=keys, **result)
    record = audit.append(settings["audit_log"], record)
    Path(settings["report_file"]).write_text(report_module.html(record), encoding="utf-8")
    if conn is not None:                           # a release: the second database keeps its own copy
        import psycopg

        from . import target as target_module
        try:
            target_module.log_run(conn, record)
        except psycopg.Error:
            print("NOTE: the second database has no run log yet, so this run is recorded in the file only.\n"
                  "      Run 'python scripts/setup_target.py' again as administrator to add it.")
    print()
    print("\n".join(report_module.text(record)))
    print(f"  audit log: line {record['line']} of {settings['audit_log']}; report to open in a browser: {settings['report_file']}")
    return record


def cmd_protect(args) -> int:
    book = _load()
    _audit_ready(book)
    key = _hash_key(args)
    rows = _read_rows(book, args.from_csv)
    facts = {"source": _source_label(args.from_csv), "keys": _key_ids(args, key),
             "destination": f"file {Path(args.out).name}" if args.out else "screen only"}
    try:
        protected, report = pipeline_module.anonymize(rows, book, key)
    except (groups_module.GateError, pipeline_module.LeakError) as error:
        _record_run("protect", book, rows, error=error, **facts)
        return _stopped(error)

    print(f"{len(rows)} rows in, {len(protected)} rows out; minimum group size {report.minimum}")
    print("\n".join(report.lines()))
    print("leak scan: 0 raw identifying values found in the output")
    print("\nfirst rows of the output:")
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(protected.head(args.rows).to_string(index=False))
    if args.out:
        protected.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\nsaved {len(protected)} rows to {args.out}")
    _record_run("protect", book, rows, protected=protected, group_report=report, written=bool(args.out), **facts)
    return 0


def cmd_release(args) -> int:
    from . import connection as connection_module   # these need the database driver
    from . import target as target_module

    book = _load()
    _audit_ready(book)
    key = _hash_key(args)
    try:
        db_key = target_module.load_db_key()
    except target_module.DbKeyError as error:
        sys.exit(str(error))
    print(f"database key: from {target_module.DB_KEY_ENV}, key ID {keys_module.key_id(db_key)}")
    target_dsn = os.environ.get("TARGET_DSN")
    if not target_dsn:
        sys.exit("TARGET_DSN is not set (connection string for anon_writer on the second database)")

    rows = _read_rows(book, args.from_csv)
    conn = connection_module.open_connection(target_dsn)
    print(f"second database: {connection_module.describe(conn)}")
    problem = target_module.layout_problem(conn, book)
    if problem:
        conn.close()
        sys.exit(f"STOPPED: {problem}\nNothing was written.")
    facts = {"source": _source_label(args.from_csv), "conn": conn,
             "destination": f"database {conn.info.dbname} on {conn.info.host}",
             "keys": _key_ids(args, key, database_key=keys_module.key_id(db_key))}
    try:
        try:
            protected, report = pipeline_module.anonymize(rows, book, key)
            written = target_module.write(conn, protected, book, db_key, report.groups, report.smallest_group)
        except (groups_module.GateError, pipeline_module.LeakError) as error:
            _record_run("release", book, rows, error=error, **facts)
            return _stopped(error, previous=True)
        print(f"{len(rows)} rows in, {written['rows']} rows written as {written['blocks']} encrypted blocks")
        print("\n".join(report.lines()))
        print("leak scan: 0 raw identifying values found in the output")
        print(f"researchers read it through the view '{book.output_table}' after:  "
              f"SET {target_module.KEY_SETTING} = '<the database key>';")
        _record_run("release", book, rows, protected=protected, group_report=report, written=True, **facts)
    finally:
        conn.close()
    return 0


def cmd_keys(_args) -> int:
    results = keys_module.audit(Path.cwd())
    for what, passed, detail in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {what}  ({detail})")
    failed = sum(1 for _, passed, _ in results if not passed)
    print("keys look right" if not failed else f"{failed} problem(s): fix them before a release")
    return 1 if failed else 0


def _ask_new_passphrase() -> str:
    from . import export as export_module
    first = getpass.getpass(f"Choose a passphrase for the private key (at least {export_module.MIN_PASSPHRASE} characters, hidden): ")
    if len(first) < export_module.MIN_PASSPHRASE:
        sys.exit("that passphrase is too short")
    if getpass.getpass("Type it again: ") != first:
        sys.exit("the two passphrases do not match")
    return first


def cmd_researcher_keygen(args) -> int:
    from . import export as export_module          # needs the cryptography package

    private_path, public_path = Path(f"{args.name}_private.pem"), Path(f"{args.name}_public.pem")
    for path in (private_path, public_path):
        if path.exists():
            sys.exit(f"{path} already exists; choose another --name or move the old file away")
    private_pem, public_pem = export_module.new_keypair(_ask_new_passphrase())
    private_path.write_bytes(private_pem)
    public_path.write_bytes(public_pem)
    print(f"private key: {private_path}  (keep it on this machine; never send it to anyone)")
    print(f"public key:  {public_path}  (send this file to the data owner)")
    print(f"fingerprint: {export_module.fingerprint(public_pem)}  (read it out to the data owner so they can check they got your key)")
    return 0


def cmd_export(args) -> int:
    from . import export as export_module

    book = _load()
    _audit_ready(book)
    try:
        public_pem = Path(args.to).read_bytes()
        recipient = export_module.fingerprint(public_pem)
        print(f"recipient: public key with fingerprint {recipient}")
    except (OSError, export_module.ExportError) as error:
        sys.exit(f"cannot use {args.to} as the researcher's public key: {error}")
    key = _hash_key(args)
    rows = _read_rows(book, args.from_csv)
    facts = {"source": _source_label(args.from_csv), "keys": _key_ids(args, key),
             "destination": f"sealed export {Path(args.out).name} for the key {recipient}"}
    try:
        protected, report = pipeline_module.anonymize(rows, book, key)
    except (groups_module.GateError, pipeline_module.LeakError) as error:
        _record_run("export", book, rows, error=error, **facts)
        return _stopped(error)
    Path(args.out).write_bytes(export_module.seal_table(protected, public_pem, book.version))
    print(f"{len(rows)} rows in, {len(protected)} rows sealed into {args.out}")
    print(f"release gate: {report.gate}; smallest group has {report.smallest_group} people")
    print("Only the holder of the matching private key can open this file. This tool cannot.")
    _record_run("export", book, rows, protected=protected, group_report=report, written=True, **facts)
    return 0


def cmd_report(args) -> int:
    from . import audit
    from . import report as report_module

    book = _load()
    settings = book.risk_report
    try:
        records = audit.read(settings["audit_log"])
    except audit.AuditError as error:
        sys.exit(str(error))
    if not records:
        sys.exit(f"{settings['audit_log']} has no runs yet. Run protect, release or export first.")
    number = args.line or len(records)
    if not 1 <= number <= len(records):
        sys.exit(f"there is no line {number}; the log has {len(records)} line(s)")
    record = records[number - 1]
    Path(settings["report_file"]).write_text(report_module.html(record), encoding="utf-8")
    print(f"run {number} of {len(records)}: {record['command']} at {record['at']} by {record['who']['user']}")
    print("\n".join(report_module.text(record)))
    print(f"  report to open in a browser: {settings['report_file']}")
    return 0


def cmd_audit(args) -> int:
    from . import audit

    book = _load()
    path = book.risk_report["audit_log"]
    intact, count, problem = audit.verify(path)
    if count:
        for record in audit.read(path)[-args.last:]:
            after = record["measure"]["after"]
            result = "passed" if record["outcome"] == "passed" else "STOPPED"
            print(f"  {record['line']:>3}  {record['at']}  {record['who']['user']:<12} {record['command']:<8} {result:<8}"
                  f" rows {record['rows']['in']} -> {record['rows']['out'] or '-'}"
                  f"  in small groups {record['measure']['before']['percent']}% -> "
                  f"{'-' if after is None else str(after['percent']) + '%'}  {record['hash'][:12]}")
    print(f"{path}: {count} run(s); {'chain intact: no line was changed or removed' if intact else 'DAMAGED: ' + problem}")
    if args.db and intact:
        import psycopg

        from . import target as target_module
        try:
            with _admin_connection() as conn:
                same, detail = audit.compare(path, target_module.run_hashes(conn))
        except psycopg.Error as error:
            sys.exit(f"could not read the run records in the second database: {str(error).splitlines()[0]}\n"
                     "If the database was set up before step 8, run 'python scripts/setup_target.py' again.")
        print(f"second database: {detail}" if same else f"second database: MISMATCH: {detail}")
        intact = same
    return 0 if intact else 1


def cmd_open_export(args) -> int:
    from . import export as export_module

    passphrase = getpass.getpass("Passphrase of the private key (hidden): ")
    try:
        table, info = export_module.open_table(Path(args.file).read_bytes(), Path(args.key).read_bytes(), passphrase)
    except export_module.ExportError as error:
        print(f"Could not open the file: {error}")
        return 2
    table.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"opened: {info['rows']} rows, made {info['created']}, cookbook version {info['cookbook_version']}")
    print(f"saved to {args.out}")
    return 0


def _admin_connection():
    """An administrator connection to the second database. The password is typed, never stored."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from . import connection as connection_module

    database = os.environ.get("TARGET_DB", "anonymized")
    dsn = os.environ.get("ADMIN_DSN")
    if dsn:
        params = conninfo_to_dict(dsn)
    else:
        params = {"host": os.environ.get("PGHOST", "localhost"), "port": os.environ.get("PGPORT", "5432"),
                  "user": os.environ.get("PGUSER", "postgres")}
        params["password"] = getpass.getpass(
            f"Password for PostgreSQL user '{params['user']}' on {params['host']}:{params['port']} (typing is hidden): ")
    return connection_module.open_connection(make_conninfo(**{**params, "dbname": database}), autocommit=True)


def _when(moment) -> str:
    """A moment as UTC text. The daily count also runs on UTC, so every time shown is on the same clock."""
    import datetime as dt
    return "never" if moment is None else moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def cmd_researcher(args) -> int:
    import psycopg

    from . import researchers
    from . import target as target_module

    book = _load()
    try:
        conn = _admin_connection()
    except psycopg.OperationalError as error:
        sys.exit(f"could not connect as administrator: {str(error).splitlines()[0]}\n"
                 "Check the password and PGPORT, and that 'python scripts/setup_target.py' has been run.")
    try:
        if args.action == "add":
            made = researchers.add(conn, args.name, book, args.days)
            print(f"account:   {made['account']}")
            print(f"password:  {made['password']}   (shown once and not saved; give it to the researcher privately)")
            print(f"expires:   {_when(made['expires'])}")
            print(f"limits:    {made['daily_query_limit']} queries a day, read-only")
            print("The researcher connects with any SQL client, for example:")
            print(f"  psql -h {made['host']} -p {made['port']} -U {made['account']} -d {made['database']}")
            print(f"and then runs:  SET {target_module.KEY_SETTING} = '<the database key>';")
            warning = researchers.unchecked_logins_text(researchers.unchecked_logins(conn) or {"rules": []})
            if warning:
                print("\nWARNING: until this is fixed, the password and the expiry date of this account protect nothing.")
                print("\n".join("  " + line for line in warning))
        elif args.action == "renew":
            print(f"{researchers.account_name(args.name)} now expires {_when(researchers.renew(conn, args.name, book, args.days))}")
        elif args.action == "remove":
            gone = researchers.remove(conn, args.name)
            print(f"{gone['account']} is removed ({gone['sessions_closed']} open session(s) closed). Its log lines are kept.")
        elif args.action == "list":
            found = researchers.accounts(conn)
            print(f"{len(found)} researcher account(s) on '{conn.info.dbname}'")
            for a in found:
                state = "EXPIRED" if a["expired"] else "active"
                if not a["made_by_tool"]:
                    print(f"  {a['account']:<24} made by hand, not with 'researcher add': it cannot read the table")
                    continue
                print(f"  {a['account']:<24} {state:<8} expires {_when(a['expires'])}   today: {a['queries_today']} counted, "
                      f"{a['logged_today']} logged   last read {_when(a['last_read'])}")
            if any(a["queries_today"] > a["logged_today"] for a in found):
                print("  (counted above logged = attempts that failed, were refused, or were rolled back by the researcher)")
        else:
            lines = researchers.log(conn, args.name, args.last)
            print(f"{len(lines)} newest line(s) of the access log, newest first")
            for at, account, number, query in lines:
                print(f"  {_when(at)}  {account:<20} #{'-' if number is None else number:<4} {' '.join(query.split())[:160]}")
    except researchers.AccountError as error:
        print(f"STOPPED: {error}")
        return 2
    finally:
        conn.close()
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):        # Windows consoles: never crash on Armenian text
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="anonymizer", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").set_defaults(run=cmd_check)
    commands.add_parser("discover").set_defaults(run=cmd_discover)
    commands.add_parser("keygen").set_defaults(run=cmd_keygen)
    commands.add_parser("keys").set_defaults(run=cmd_keys)
    sub = commands.add_parser("researcher-keygen")
    sub.add_argument("--name", required=True, help="used for the two file names, for example: anna")
    sub.set_defaults(run=cmd_researcher_keygen)
    sub = commands.add_parser("researcher", help="researcher accounts on the second database")
    sub.add_argument("action", choices=("add", "renew", "remove", "list", "log"))
    sub.add_argument("name", nargs="?", help="the researcher's short name, for example: anna")
    sub.add_argument("--days", type=int, help="days until the account expires (default: account_days in the cookbook)")
    sub.add_argument("--last", type=int, default=20, help="how many log lines to show (default 20)")
    sub.set_defaults(run=cmd_researcher)
    sub = commands.add_parser("report", help="show the report of the latest run again")
    sub.add_argument("--line", type=int, help="the run's line number in the audit log (default: the latest)")
    sub.set_defaults(run=cmd_report)
    sub = commands.add_parser("audit", help="list the audit log and check its chain")
    sub.add_argument("--last", type=int, default=10, help="how many runs to list (default 10)")
    sub.add_argument("--db", action="store_true", help="also compare with the run records in the second database")
    sub.set_defaults(run=cmd_audit)
    sub = commands.add_parser("open-export")
    sub.add_argument("file", help="the export file to open")
    sub.add_argument("--key", required=True, metavar="FILE", help="your private key file")
    sub.add_argument("--out", required=True, metavar="FILE", help="where to save the table as CSV")
    sub.set_defaults(run=cmd_open_export)
    for name, run in (("prepare", cmd_prepare), ("transform", cmd_transform), ("protect", cmd_protect),
                      ("release", cmd_release), ("export", cmd_export)):
        sub = commands.add_parser(name)
        sub.add_argument("--from-csv", metavar="FILE", help="read this CSV instead of the source database")
        sub.set_defaults(run=run)
        if name in ("transform", "protect"):
            sub.add_argument("--out", metavar="FILE", help="also save the output as a CSV")
            sub.add_argument("--rows", type=int, default=5, help="how many output rows to print (default 5)")
        if name in ("protect", "release", "export"):
            sub.add_argument("--one-time-hash-key", action="store_true",
                             help="use a hash key made for this run only, never shown or stored")
        if name == "export":
            sub.add_argument("--to", required=True, metavar="FILE", help="the researcher's public key file")
            sub.add_argument("--out", required=True, metavar="FILE", help="the sealed export file to write")
    args = parser.parse_args()
    if args.command == "researcher" and args.action in ("add", "renew", "remove") and not args.name:
        parser.error(f"researcher {args.action} needs a name, for example: researcher {args.action} anna")
    if args.command == "researcher" and args.days is not None and args.days < 1:
        parser.error("--days must be at least 1")
    try:
        return args.run(args)
    except Exception as error:                     # the TLS rule refused a connection: say so plainly
        if type(error).__name__ == "InsecureConnectionError":
            return _stopped(error)
        raise


if __name__ == "__main__":
    sys.exit(main())
