# Anonymizer: roadmap steps 1 to 9

Everything here works on the mock data only.

- **Step 1** is `cookbook.yaml`: one rule per source column, and the output table.
- **Step 2** connects to the source database read-only, matches its columns to the cookbook, cleans location text and marks the person behind each card.
- **Step 3** applies the column rules and produces the output table in memory.
- **Step 4** protects small groups: it merges small places, generalizes small combinations, checks dominance, and refuses to release a table that still has a group below 5.
- **Step 5** writes the finished table into a second database, where it is stored encrypted and read through a read-only view.
- **Step 6** keeps the keys with their owners: a key check, a one-time hash key, and export files sealed so that only one researcher can open them.
- **Step 7** gives each researcher their own read-only account with an expiry date, a log of every query and a daily limit.
- **Step 8** writes a one-screen risk report for every run and keeps every run in an audit log that shows if it was altered.
- **Step 9** scores the tool against the 21 planted scenarios and attacks its own output.

## Check that it works

```bash
pip install -r requirements.txt
python check.py
```

This needs only Python. It prints one PASS or FAIL line per check and ends with a RESULT line;
`ALL 102 CHECKS PASSED` means the cookbook, the location cleaning, the column rules, the small-group protection, the sealed exports, the risk report, the audit log and the scenario checks work.

To also check the database side (read-only account, column matching), run:

```bash
python check.py --db
```

It asks for the password of the PostgreSQL user `postgres` (typing is hidden, nothing is stored) and
should end with `ALL 147 CHECKS PASSED`. It connects to `localhost:5432`, database `postgres`; set
`PGHOST`, `PGPORT`, `PGUSER` or `PGDATABASE` first to use something else. Never put the password in a file.

The database check rebuilds the table `public.bills`, so never point it at a database you care about.
It also creates a scratch database `anonymized_check` for the step 5, 7 and 8 checks and removes it afterwards.
For step 7 it makes two accounts of its own (`res_check_anna`, `res_check_boris`) and deletes them.
It changes no existing account's password.

## See the anonymized output

```powershell
python -m anonymizer keygen                      # prints a new secret key and the line to set it
$env:ANON_HASH_KEY = "<the key it printed>"
python -m anonymizer protect --from-csv data/mock_ena_bills.csv --out anonymized.csv
```

`protect` runs steps 2 to 4 in one go. It prints the buckets it made, what it generalized, what it withheld
and the release-gate result, then saves the table to `anonymized.csv`, which opens in Excel. If the gate
fails it prints why and saves nothing. Leave out `--from-csv ...` to read the source database instead
(needs `SOURCE_DSN`). `python -m anonymizer transform ...` shows the result of step 3 alone.

## Write the encrypted second database (step 5)

Once, as the database administrator (PowerShell; use your own port):

```powershell
$env:ADMIN_DSN = "postgresql://postgres:<admin password>@localhost:5433/postgres"
$env:READER_PASSWORD = "<choose one>"; $env:WRITER_PASSWORD = "<choose another>"
python scripts/setup_source.py data/mock_ena_bills.csv     # the stand-in source, with its read-only account
python scripts/setup_target.py                             # the second database "anonymized"
Remove-Item Env:ADMIN_DSN
```

Every release, as the tool:

```powershell
python -m anonymizer keygen                                # once: prints ANON_HASH_KEY and ANON_DB_KEY lines to paste
$env:SOURCE_DSN = "postgresql://anon_reader:<reader password>@localhost:5433/postgres"
$env:TARGET_DSN = "postgresql://anon_writer:<writer password>@localhost:5433/anonymized"
python -m anonymizer release
```

`release` runs steps 2 to 5. If the release gate or the leak scan fails, it writes nothing and the previous
release stays in place.

A researcher needs their own account (made in step 7 below) and the database key.
Then, in any SQL client connected to the database `anonymized`:

```sql
SET anonymizer.db_key = '<the database key>';
SELECT region, count(*), sum(kwh_day + kwh_night) FROM bills_anonymized GROUP BY region;
```

How the encryption works:

- The table is stored in `bills_anonymized_store` as blocks of 200 rows, each encrypted with AES-256 (PostgreSQL's pgcrypto, OpenPGP format). The database files and any backup hold only ciphertext.
- `bills_anonymized` is a view. It decrypts the blocks for a session that has set the right key. No key gives an error, and so does a wrong key.
- The database key is never stored: not in the database, not in the cookbook, not in a file. It is read from `ANON_DB_KEY` when writing and set by each researcher's own session when reading.
- Researchers can read the view and nothing else. They cannot see the stored blocks, write anything, or reach the source.
- The tool's account `anon_writer` can replace the stored blocks but cannot read the view.
- A release replaces everything in one transaction, so a reader sees the old table or the new one, never half.
- Connections to another machine must be encrypted (TLS) or the tool refuses. On this machine an unencrypted connection is allowed with a warning; set `ANON_REQUIRE_TLS=1` to refuse that too.
- Statement logging is switched off for the second database, so a `SET anonymizer.db_key` line cannot end up in the server log. Who read the table is recorded by the tool's own access log instead (step 7), with the key removed.

Limits to say out loud:

- While a query runs, the server holds the key and the decrypted rows in memory. This protects the files, backups and anyone without the key; it does not hide the data from the server's own administrator during a query.
- Every query decrypts all blocks, which is fine for a demo and slow for millions of rows.

## Keys and sealed exports (step 6)

Check the two keys before a release:

```powershell
python -m anonymizer keys
```

It confirms both keys are set, long enough, different from each other, and not written in any file of this
folder. It shows a short key ID for each, so two people can confirm they hold the same key without reading it out.

For a release whose subscriber IDs can never be rebuilt or linked to another release, add
`--one-time-hash-key` to `protect`, `release` or `export`. The hash key is then made in memory for that run
and never shown.

A sealed export is a file that only one researcher can open (Telegram's secret-chat model):

```powershell
# 1. The researcher, on their own machine. The private key never leaves it.
python -m anonymizer researcher-keygen --name anna
#    -> anna_private.pem (stays with her), anna_public.pem (she sends this), and a fingerprint to read out

# 2. The data owner, after checking the fingerprint with her by phone.
python -m anonymizer export --to anna_public.pem --out anna.anonexport --from-csv data/mock_ena_bills.csv

# 3. The researcher opens it. Nobody else can, including this tool and the data owner.
python -m anonymizer open-export anna.anonexport --key anna_private.pem --out data.csv
```

How a file is sealed: a one-time key pair is made for the file and combined with the researcher's public key
(X25519) into an AES-256 key (HKDF-SHA256); the table is encrypted with AES-256-GCM, and the one-time private
half is thrown away. Any change to the file is detected when it is opened. The export passes the same release
gate and leak scan as a database release; if either fails, no file is written.

Who can read what:

| | Second database | Sealed export |
|---|---|---|
| Model | Telegram cloud chat | Telegram secret chat |
| Who can read | A researcher with their own account and the database key | Only the one researcher holding the private key |
| Can the server read it? | Yes, while answering a query | No |
| Can it be queried with SQL? | Yes | No, it is a file |

## Researcher accounts (step 7)

If the second database was created before step 7, update it once (it keeps the stored release):

```powershell
$env:ADMIN_DSN = "postgresql://postgres:<admin password>@localhost:5433/postgres"
$env:WRITER_PASSWORD = "<the writer password>"
python scripts/setup_target.py
Remove-Item Env:ADMIN_DSN
```

Then, as the database administrator. Each command asks for the `postgres` password (hidden); set `PGPORT` first:

```powershell
$env:PGPORT = "5433"
python -m anonymizer researcher add anna        # her own account res_anna; prints its password once
python -m anonymizer researcher list            # every account: expiry, queries today, last read
python -m anonymizer researcher log             # newest lines of the access log; add a name for one account
python -m anonymizer researcher renew anna      # move the expiry date forward (--days 90 for another length)
python -m anonymizer researcher remove anna     # close her sessions and delete the account, in one step
```

The researcher connects as `res_anna` with that password, sets the database key, and queries the view.

What each account gets:

- **Its own name.** One account per researcher. An account made by hand, outside this command, can log in but reads nothing.
- **Read-only.** It can read the view `bills_anonymized` and `release_info`. It cannot write, cannot see the stored blocks, the access log or its own counter, and has no rights on the source table.
- **An expiry date** (30 days, `account_days`). After it the account cannot log in, and a session left open is refused too.
- **A log line for every query**: account, time, the number of the query that day, and the query text. Only the administrator can read it. The database key, long quoted values and anything that looks like a key are replaced before the text is stored; short values such as `'resident'` stay readable, so an auditor can see who was narrowing in on what.
- **A daily limit** of 200 queries (`daily_query_limit`), counted per account and per day in UTC. The count cannot be undone by rolling a query back, and wrong-key attempts count too, so the key cannot be guessed at speed.
- **A time limit** of 60 seconds per query and at most 3 open sessions.

The limits live in `cookbook.yaml` under `second_database.researchers`. After changing them, run
`scripts/setup_target.py` again; `account_days` and `connections` apply to accounts made afterwards.

How it works: the view asks one function for the key, once per query. That function runs with the
administrator's rights, so it can check the expiry date, move the counter and write the log, while the
researcher's own account can touch none of them.

Limits to say out loud:

- A researcher who rolls back their own transaction removes that query's line from the log table. The count is still taken, and the server log keeps a line `anonymizer access: account=... query_no=...` (without the query text), so `researcher list` shows more queries counted than logged.
- The limit counts queries, not rows: one query can return the whole table. It slows bulk and repeated pulling and makes it visible; it does not stop a researcher from copying a release they are allowed to read.
- The 60-second limit protects the server from heavy queries. It is not a privacy control.

### If the server does not check passwords

`python check.py --db` fails "a wrong password is refused" when PostgreSQL is set to let connections from
this machine in without a password (the method `trust` in its `pg_hba.conf`). Accounts, passwords and expiry
dates then protect nothing on this machine, for the administrator account too. The check prints the file and
the lines. To fix it:

1. Give `postgres` a password you know, while you can still get in. `psql` asks for it twice, hidden:
   `psql -h localhost -p 5433 -U postgres -c "\password postgres"`
   (if `psql` is not found, it is in the `bin` folder of the PostgreSQL installation).
2. Open the file the check printed, as administrator, and on the listed lines change `trust` to the method
   the check printed (`scram-sha-256` on current versions). Save.
3. Reload: `psql -h localhost -p 5433 -U postgres -c "SELECT pg_reload_conf()"`, or restart the PostgreSQL service.
4. Run `python check.py --db` again, typing the password from step 1.

If you lock yourself out, put `trust` back on those lines and restart the service.

## Risk report and audit log (step 8)

Every `protect`, `release` and `export` run now ends with a short risk report, writes `report.html`, and adds
one line to `audit_log.jsonl`. Both files appear in the folder the command is run from.

```powershell
python -m anonymizer protect --from-csv data/mock_ena_bills.csv    # ... ends with the RISK REPORT lines
start report.html                                                  # the one-screen report, in the browser
python -m anonymizer audit                                         # every run so far, and whether the log is intact
python -m anonymizer audit --db                                    # also compare with the copy in the second database
python -m anonymizer report --line 3                               # show an earlier run's report again
```

The headline number is the share of subscribers who sit in a group smaller than 5 when grouped by place,
consumer type and tariff group. On the mock data it goes from 40.7% (70 of 172) before the run to 0% after.
It is counted the same way the release gate counts: distinct people, in a group's smallest month.

What the report shows on one screen: the headline before and after, PASSED or STOPPED (with the reason),
rows and columns in and out, how many places are still named, the smallest group, how many subscribers were
withheld, the leak scan, each bucket with its total, and who ran what, when, with which cookbook and key IDs.

What a record never holds: a raw value from the source, a key, a password, or the name or size of a place that
was hidden. Buckets appear with their total only. The gate's error message gives counts, not the group's name.

The audit log:

- One line per run, including runs that were stopped. Each line holds the fingerprint of the line before it,
  so a changed, removed, added or reordered line breaks the chain, and `audit` says at which line.
- A damaged log stops the tool before it does anything. Keep the damaged file and start a new one.
- A chain cannot show that the newest lines were cut off the end. So every release attempt is also stored in
  the second database, in the table `run_log`. The tool's account can add a line there and nothing else: it
  cannot read, change or delete one, or choose the time and account stored with it. `audit --db` compares the two.
- If the second database was created before step 8, run `python scripts/setup_target.py` once more to add `run_log`.

Limits to say out loud:

- The headline treats the three residential consumption bands as one tariff group, because a band is the
  measured consumption itself, not something a stranger knows from outside. Counting each band separately
  gives 72.1% before and 29.8% after; that stricter count is kept in every record as `without_merging_bands`.
- Whoever controls both the folder and the database administrator account can rewrite both logs. The log
  protects against the tool's operator, not against the server's owner.
- `protect` and `export` runs are in the file only; just releases have the copy in the database.

## Scoreboard: scenarios and self-attack (step 9)

```powershell
python score.py          # the 21 scenarios and the attack; writes scoreboard.html
python score.py --db     # also the 3 access checks (asks for the postgres password, like check.py)
python score.py --full   # also measure the known limit below (the paper-bill row)
start scoreboard.html
```

Run `python check.py` and `python score.py` after every change to the cookbook or the engine.

- **Scenarios.** Each of S01 to S21 from the Scenarios sheet is one check on the finished table. All 21 pass.
  A check that does not pass is shown as OPEN, with its reason and its fix printed next to it.
- **Access.** A researcher cannot write, cannot reach the source, and the stored data cannot be read without the key.
- **Attack.** For one kind of outside knowledge at a time, the scoreboard counts how many subscribers that
  knowledge singles out in the output. It measures what the cookbook actually publishes: put a column back
  into the output and its row returns to the table.

What the attack found on the first run (cookbook version 1, 168 subscribers), and after the fixes (version 2):

| If someone knows... | Version 1 | Version 2 | What closed it |
|---|---|---|---|
| where they live, the consumer type and the voltage | 0 | 0 | step 4 |
| the same, and whether they have solar panels | 3 | gone | `has_solar` deleted |
| the same, and the type of meter | 6 | gone | `meter_type` deleted |
| the same, and the day the meter was read in one month | 39 | gone | `reading_date` deleted |
| the same, and how they paid in one month | 53 | gone | `payment_channel` deleted |
| the same, and the year the contract was signed | 105 | 0 | 10-year bands, inside the group check |
| the same, and which substation feeds them | 0, in groups as small as 3 | 0 | substation inside the group check |
| the amount paid in one month, from a receipt | 160 | gone | `amount_paid_amd` deleted |
| a bill number, as printed on any bill | 168 | gone | `bill_id` is now a keyed hash |
| where they live and their true kWh in one month, from a paper bill | about 153 | about 150 | still open |

The scoreboard names this last row as a known limit in one line and measures it only with `--full`.
It cannot be closed by a column rule: it is the price of publishing bill-level rows.
Someone who already knows one household's exact consumption in one month can find that household's row, and
then learns its other months. Stronger noise, wider bins, or group totals instead of rows reduce it.

What version 2 costs in detail: the output has 14 columns instead of 19. The substation is shown on 162 of 505
rows and the contract decade on 106, because each is blank wherever it would describe fewer than 5 people
together with the place, consumer type and voltage. To keep more decades and fewer substations, swap the two
names in `small_groups.also_protect`.

After a change to the output columns, update the second database before the next release:
`python scripts/setup_target.py`. A release stops with that message if the database still has the old layout.

## Files

| File | What it is |
|---|---|
| `cookbook.yaml` | One rule per source column, the parameters, and the output table |
| `anonymizer/cookbook.py` | Loads the cookbook and refuses one that contradicts itself |
| `anonymizer/source.py` | Read-only connection, column discovery, column matching |
| `anonymizer/prepare.py` | Location cleaning, location key, person marker |
| `anonymizer/transform.py` | The column rules, and the leak scan that searches the output for raw values |
| `anonymizer/groups.py` | Small-group protection: buckets, combination check, dominance, release gate |
| `anonymizer/pipeline.py` | The whole run in order: anonymize, check, and only then write |
| `anonymizer/connection.py` | The connection rule: encrypted, except on this machine |
| `anonymizer/target.py` | The second database: encrypted blocks, the decrypting view and its gatekeeper function, writing a release |
| `anonymizer/export.py` | Sealed exports: researcher key pairs, sealing, opening |
| `anonymizer/researchers.py` | Researcher accounts: add, renew, remove, list, and reading the access log |
| `anonymizer/report.py` | The risk report: the headline measure, the record of a run, the one-screen page |
| `anonymizer/audit.py` | The audit log: chained lines, checking the chain, comparing with the database copy |
| `anonymizer/scenarios.py` | The 21 scenario checks, the attack on the output, and the scoreboard page |
| `score.py` | One command that prints the scoreboard and writes `scoreboard.html` |
| `anonymizer/keys.py` | The key check, key IDs, and the one-time hash key |
| `anonymizer/__main__.py` | All commands; run `python -m anonymizer --help` for the list |
| `check.py` | One command that reports PASS or FAIL for all steps so far |
| `scripts/setup_source.py` | Builds the stand-in source database from the mock CSV |
| `scripts/setup_target.py` | Creates or updates the second database: accounts, encrypted store, view, access log, run log |
| `tests/` | The "done when" checks, for `pytest` |

## Decisions built in

- Columns on the agreed list follow the list. Of the columns not on it, only `voltage_kv` is copied unchanged
  (the group check covers it). After the attack of step 9, `bill_id` is hashed and `notes`, `amount_paid_amd`,
  `has_solar`, `meter_type`, `payment_channel` and `reading_date` are deleted.
- The contract date becomes its decade, such as `2010-2019`.
- A source column the cookbook does not name is reported and left out. A cookbook column missing from the source stops the run.
- The hash key comes only from the `ANON_HASH_KEY` environment variable, and must be at least 32 characters.
  The same key gives the same IDs in every run; a new key gives new IDs.
- Card, meter and bill numbers become 16-character hashes. The column name is mixed in, so the two can never be matched to each other.
- Every kWh value is first moved by a random amount of up to 5% in either direction (`kwh_noise_percent`). The noise is computed from the secret key, the card and the month, so the same bill always gets the same noise and it cannot be averaged away by running the tool twice. Totals over many bills barely change.
- kWh is then rounded to the nearest 10. Household bills (consumer type `resident`) are capped at the 99th percentile; businesses are not capped.
- A bill of 50 kWh or less (day + night, `kwh_lowest_bin`) is published as 50 day and 0 night, so an empty home with 0 kWh looks like any other very small bill.
- The `social` tariff becomes the residential tier that the bill's kWh falls into.
- A substation is shown only if it serves at least 5 people; otherwise the cell is blank. Step 4 then checks it again inside each group.
- `billing_month` is kept as is. Timestamps finer than an hour would be cut to the hour.
- The person marker (passport or ID for people, taxpayer number for companies) exists only in memory.

How step 4 decides (minimum group size 5, counted in people, smallest month):

- A settlement below 5 is merged with its community's other small settlements into "Other settlements, X".
- A bucket needs at least 5 people from at least 2 places. If it falls short, the next-smallest neighbour is pulled in, so a hidden place cannot be found by subtraction. If that swallows everything, only the community is shown.
- The same happens one level up: small communities share "Other communities, X", small regions share "Other regions".
- A blank place never pulls a named place in; it is shown as "Not shown".
- Place + consumer type + voltage must also describe 5 people. If not: legal entities and sole proprietors merge into "business", high voltages merge, then the place is shown less precisely ("Not shown"), and as a last resort the subscriber is withheld and reported.
- If one member holds more than half of a group's kWh, that member's kWh is blanked.
- Every decision is made once per subscriber, so nobody changes group between months.
- A settlement that is no longer named also loses its substation.
- Substation and contract decade (`also_protect`) are each shown only where place + consumer type + voltage + the column itself still describe 5 people. A small value is never hidden alone: if the hidden ones add up to fewer than 5, the next-smallest value in that group is hidden with them. The release gate checks all of these columns together.

## Open points for the team

- Someone who knows a household's true kWh in one month can still find its row (see the scoreboard). This is
  inherent in bill-level data and is the one attack left open.
- A home in the lowest kWh bin in every month still stands out as "always very low", though no longer as exactly 0.
- The residential tariff tier is computed from the bill's real kWh, before noise and rounding. It adds nothing
  an attacker with the paper bill does not have, but it is slightly more exact than the published kWh.
- On the mock data only 7 of 29 places keep their name, and the contract decade survives on a fifth of the rows,
  because the mock is built from tiny places. Real data will keep far more.

## For the next steps

The features are frozen: every scenario passes and the one open attack has its written reason. Step 10 is the demo and the pitch.
