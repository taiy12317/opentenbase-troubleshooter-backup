# otb-preflight

Read-only environment preflight for OpenTenBase v5.0 (Postgres-XL architecture)
deployment. One file, zero third-party dependencies (Python 3.9+ stdlib only).

Checks everything that commonly breaks an OpenTenBase install or start-up:

| Check | What it catches |
|---|---|
| `env.ld_library_path` | `LD_LIBRARY_PATH` unset or missing compat/runtime dirs → `libreadline.so.7` / `libssl.so.1.1` load failure (exit 127) |
| `bin.tools` | Missing or non-executable `postgres` / `psql` / `gtm_ctl` / `pg_ctl` |
| `dynlib.ldd` | Unresolved dynamic libraries of `postgres` via `ldd` |
| `libpq.symlink` | `runtime/libpq/libpq.so.5` not pointing at `pg/lib/libpq.so.5.10` |
| `port.check` | Ports already occupied (pre-deploy) or expected ports not listening (`--expect-running`) |
| `dir.check` | Missing data/log/config directories and files |
| `disk.space` | Free disk below `--disk-min-free-gb` |
| `conf.duplicate_guc` | Same GUC defined multiple times in `postgresql.conf` (last one wins) |
| `auth.pg_hba_trust` | `trust` authentication methods in `pg_hba.conf` (supports inline comments, quoted fields and legacy separate netmask form; not a full syntax validator) |
| `db.probe` | Optional `psql SELECT 1` probe via `--conn`; conninfo is bound as one `--dbname=<conninfo>` argument and PASS requires output exactly `1`; exit 127 (missing shared libs vs psql not in PATH), connection refused, role/database errors are classified |

All checks are read-only. Nothing is created, modified, started or stopped.

## Usage

```bash
# Pre-deploy check (database not running is expected)
python3 otb_preflight.py

# Post-deploy check: database should be up and reachable
python3 otb_preflight.py --expect-running \
  --conn "host=127.0.0.1 port=15001 dbname=lab user=student"

# Point at real config files to enable duplicate-GUC / pg_hba checks
python3 otb_preflight.py \
  --conf-files /data/opentenbase-lab/cn/data/postgresql.conf \
  --pg-hba-files /data/opentenbase-lab/cn/data/pg_hba.conf

# Reproduce a dynamic-library failure without touching the real environment
# (probe_env only affects the psql child process)
python3 otb_preflight.py --probe-env "LD_LIBRARY_PATH=/wrong/path" \
  --conn "host=127.0.0.1 port=15001"
```

Options:

| Option | Default | Meaning |
|---|---|---|
| `--pg-bin` | `/opt/opentenbase-lab/pg/bin` | PostgreSQL binaries directory |
| `--runtime-libpq` | `/opt/opentenbase-lab/runtime/libpq` | runtime libpq directory |
| `--compat-dirs` | lab compat dirs | comma-separated compat library dirs |
| `--data-dirs` | lab data dirs | comma-separated data directories |
| `--log-dir` | `/data/opentenbase-lab/logs` | log directory |
| `--ports` | `5000,15001,15401,15402` | comma-separated ports |
| `--conf-files` / `--pg-hba-files` | empty (skip) | comma-separated config paths |
| `--conn` | empty (skip probe) | libpq conninfo for the psql probe |
| `--expect-running` | off | database should be running (not-listening / refused → FAIL) |
| `--probe-env K=V` | none | env override for the probe child process only |
| `--disk-min-free-gb` | `1.0` | free-space warning threshold |
| `--timeout` | `10` | child-process timeout (seconds) |
| `--strict` | off | WARN-level items also produce exit code 1 |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No FAIL-level problems (WARN allowed; WARN counts as 1 with `--strict`) |
| 1 | At least one FAIL-level problem |
| 2 | Execution failed or incomplete (e.g. `ss` unavailable, config unreadable) |
| 3 | Argument error |

Per-item status: `PASS` / `WARN` / `FAIL` / `ERROR` / `SKIP` (condition not met,
e.g. no `--conf-files` provided — not a fault).

## Credentials

The conninfo password is redacted in all tool output (both `key=value` and
`postgresql://` URI forms, quoted or unquoted). Child-process timeouts do not
echo the command line. `--probe-env PGPASSWORD=...` values are shown as `***`.
The probe connection string is passed as a bound psql database-name value, so
option-like input cannot be interpreted as a psql option or count as a probe
success without the expected `SELECT 1` output.

## Relationship to the full toolkit

This file is the deployment-preflight subset of the
OpenTenBase Agentic Troubleshooting Toolkit (`otb-tools` CLI: `preflight` /
`inspect` / `diagnose`, plus an error-case knowledge base), reduced to a single
file for `contrib/` submission:

- Drops the psycopg2 dependency: the database probe runs `psql` as a child
  process.
- Inlines a minimal result model (no JSON output; plain text lines per item).
- Same check items, same exit-code semantics, same failure signatures
  (all reproduced on a real OpenTenBase v5.0 cluster or sourced from community
  issues; see the toolkit's `references/error-cases.md`).

## Tests

`tests/test_standalone_preflight.py` (pytest): 63 tests covering every check,
exit codes 0/1/2/3, conninfo redaction for quoted/URI forms, probe-env password
redaction, and the exit-127 disambiguation (missing shared libraries vs psql
not in PATH).

```bash
python3 -m pytest tests/test_standalone_preflight.py
```

## License

Before submitting to OpenTenBase `contrib/`, add the project's license header
at the top of the file, following the convention of existing contrib modules
(e.g. `contrib/otb-health`).
