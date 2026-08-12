---
name: dbhub-setup
description: Qualify stable DBHub npm releases once against a fixed projectless local database matrix, reuse the immutable qualified runtime for lightweight project upgrades, and set up secure project-level DBHub MCP with macOS Keychain-backed credentials, lazy read-only tools, and no plaintext passwords. Use for stable-version checks, DBHub upgrades, scheduled qualification, or project connection setup.
---

# DBHub Release Qualification and Project Setup

Qualify an exact stable DBHub artifact globally, then configure or upgrade
project-local DBHub MCP servers whose database passwords remain in macOS
Keychain.

## Verified Compatibility

This Skill has been verified with macOS and OpenAI Codex. DBHub performs the
database connections and queries; this Skill qualifies the DBHub runtime and
reduces the setup, validation, and upgrade work required in real projects.

Other operating systems and agents may adapt the credential-storage, Skill
path, and project MCP configuration portions. Treat those combinations as
unverified until their platform-specific behavior has focused tests.

Resolve `<dbhub-setup-skill-dir>` to the directory containing this `SKILL.md`
before running bundled scripts. Do not assume the Skill was installed globally
or hard-code an agent-specific Skill directory.

## Use This Skill For

- Creating project-level `.codex/config.toml` DBHub configuration.
- Checking the official npm stable release without touching a project.
- Qualifying a new stable artifact against fixed local database images.
- Scheduling a projectless weekly stable-release check.
- Applying an already-qualified runtime to an existing generated project.
- Adding named database sources such as PROD/UAT `app` and `identity`.
- Grouping multiple databases that intentionally share one host/user password.
- Guiding the user to create missing macOS Keychain items safely.
- Keeping database connections lazy and SQL tools read-only with bounded rows.
- Replacing an existing generated DBHub setup after inspecting it.

Do not use the global release qualification to inspect application data, scan
projects, access Keychain, connect to UAT/PROD, or upgrade projects
automatically. Do not use project setup to change database grants, store
passwords in files, or claim DBHub application-level readonly mode is a
database security boundary.

## Global Stable Release Qualification

The global qualification is deliberately separate from every business project:

```text
official npm metadata
        |
        v
exact artifact + locked runtime
        |
        v
fixed local Docker/SQLite matrix
        |
        v
immutable qualification credential
        |
        v
lightweight per-project upgrade
```

Use the bundled manager:

```bash
# Metadata and cache only: no package download and no containers.
python3 <dbhub-setup-skill-dir>/scripts/manage_dbhub_release.py check

# Run the heavy matrix only when no matching credential exists.
python3 <dbhub-setup-skill-dir>/scripts/manage_dbhub_release.py qualify

# Pin a specific stable version when reproducing a release.
python3 <dbhub-setup-skill-dir>/scripts/manage_dbhub_release.py \
  qualify --version '<stable-version>'
```

The candidate must be an exact stable semver from
`https://registry.npmjs.org`. The manager rejects alternate registries and
tarball origins. Every npm command runs from private qualification state with
fresh empty user/global npmrc files, an explicit private cache, a sanitized
environment, and lifecycle scripts disabled. It verifies package name,
version, `engines.node`, `dist.integrity`, `dist.shasum`, the downloaded
tarball hashes, installed package identity, and official lockfile provenance.
The exact tarball, `package-lock.json`, and installed dependency closure remain
inside a unique runtime directory.

Qualification state lives under
`~/.codex/state/dbhub-setup/`. A credential is reusable only when the exact
artifact identity, Node major, host platform/architecture, MCP client,
Testcontainers adapter/core versions, harness lock, qualification-policy
version, matrix, and probe inputs match. Reuse also recomputes a versioned hash
over the complete runtime tree, including paths, modes, file bytes, and
root-contained symlinks; entry or transitive-dependency mutation therefore
invalidates the credential. Runtime roots outside the private qualification
state are rejected. Bump the explicit policy version when artifact or
qualification semantics change; ordinary documentation edits do not invalidate
a completed matrix. The same version appearing with a different npm integrity
is `security_blocked`.
Core-contract failure produces no reusable credential. When only connector
cells fail, the manager records `partially_qualified` with explicit passing and
failing connector coverage; it never relabels failed cells as passing. Failures
never overwrite the most recent successful credential.

The matrix manifest is
`scripts/dbhub_release_matrix.json`. It pins every non-SQLite image by registry
digest, image ID, and platform. The probe refuses floating tags and never pulls
implicitly. Testcontainers' Ryuk helper is disabled so it cannot introduce an
unpinned helper image; each database container is stopped explicitly in a
`finally` path. Missing required images block qualification before any
container is started; download only the exact digest already listed in the
manifest after the user authorizes it. Never prune unrelated images, volumes,
or containers. A process killed outside normal cleanup can leave a disposable
container behind, so inspect residual qualification containers after an
interrupted run rather than pruning broadly.

Each matrix cell validates:

- actual database version;
- MCP initialize and `serverInfo.version`;
- `tools/list` and the exact single-source tool input contract:
  `execute_sql.sql` is required; `search_objects.object_type` is required with
  its fixed enum; `pattern`, `detail_level`, and `limit` retain their defaults;
  and `limit` remains an integer greater than zero and at most `1000`. A
  `$schema` declaration is accepted only when it exactly names JSON Schema
  draft 2020-12, then omitted from the normalized semantic comparison;
- `SELECT 1 AS dbhub_connection_check`;
- bounded schema search;
- `max_rows = 3` using four literal rows;
- `READONLY_VIOLATION` for an update to a nonexistent target;
- `EXECUTION_ERROR` for a missing-table read;
- successful `SELECT 1` after both failures;
- absence of synthetic credentials and the SQL marker in stderr.

The core probe also initializes a lazy two-source SQLite configuration with
legacy `2025-11-25` and modern `2026-07-28` protocol negotiation. Single-source
tools are `execute_sql` and `search_objects`; multi-source tools are
`execute_sql_<source>` and `search_objects_<source>`.

> [!IMPORTANT]
> Qualification proves the tested application behavior and locked dependency
> closure. DBHub readonly remains a SQL-classification guard. It does not prove
> that every SELECT-invocable function is harmless and never replaces database
> least privilege.

## Scheduled Check Contract

A recurring check should be projectless, local, and call only:

```bash
python3 <dbhub-setup-skill-dir>/scripts/manage_dbhub_release.py qualify
```

The command may include
`--registry https://registry.npmjs.org`; no other registry value is accepted.

The manager performs the lightweight official metadata/cache check first. When
the exact artifact and qualification-input fingerprint are already valid, it
returns `up_to_date` without downloading the package or starting Docker.
Only a new stable artifact, a materially changed qualification input, an
incomplete prior run, or an explicitly forced run executes the heavy matrix.

The scheduled task must not scan business projects, call `security`, read
Keychain, start a project launcher, connect to a real database, or modify any
project. Report `qualified`, `partially_qualified`, `up_to_date`,
`security_blocked`, `blocked`, `failed`, or `already_running` with the
credential path and sanitized connector/failure stage.

## Lightweight Project Upgrade

After global qualification, inspect one actual project instead of assuming it
has UAT and PROD:

```bash
python3 <dbhub-setup-skill-dir>/scripts/upgrade_dbhub_project.py \
  --project-root /absolute/path/to/project \
  --target '<qualified-version>'
```

The default is a dry plan. It reads only the generated launcher and TOML,
requires modes `700` and `600`, derives the connector set from the project's
actual sources, and requires every required matrix cell for each connector to
pass. It does not read passwords or connect to databases. For a legacy `npx`
launcher, also review `keychain_mapping` and copy its
`keychain_mapping_sha256`; this explicit confirmation prevents unattended
migration of an unverified credential mapping. Apply only after reviewing the
plan:

```bash
python3 <dbhub-setup-skill-dir>/scripts/upgrade_dbhub_project.py \
  --project-root /absolute/path/to/project \
  --target '<qualified-version>' \
  --apply \
  --confirm-keychain-mapping '<hash from the dry plan>'
```

Omit `--confirm-keychain-mapping` when updating an already hardened,
schema-marked launcher.

The updater migrates a recognized generated `npx` launcher to a hardened
qualified launcher, or changes the exact version in an existing qualified
launcher. It binds the launcher to the installed runner, Python interpreter,
qualification state, and SHA-256 of the generated TOML instead of accepting
environment overrides. Legacy migration accepts only the exact historical
generated template; unknown code or edits require regeneration instead of
heuristic rewriting. Before normal Keychain access, the runner validates the
credential fingerprint, complete runtime tree, TOML ownership/mode/content,
lazy sources, password placeholders, readonly/max-row tools, and connector
coverage. The same validation runs again immediately before DBHub exec. It does
not change TOML or Keychain data. Verify syntax, dummy-credential launcher
loading, fresh MCP initialize, and `tools/list`. Run per-source `SELECT 1` only
when private-system access is explicitly authorized. Adapt optional UAT/PROD
checks to the sources that actually exist; never manufacture environment tiers.

## Required Inputs

For every source, resolve these non-secret values:

- Source ID: stable lowercase identifier, for example `prod_app`.
- DBHub database type, host, port, database/schema, and username.
- Display label, for example `PROD app`.
- Keychain account: credential-group identifier, for example `prod`.

For MySQL and MariaDB sources, DBHub setup writes `timezone = "+08:00"` by
default. This makes timezone-naive `DATETIME` values consistently mean China
Standard Time instead of inheriting the host's local timezone. Use
`--mysql-timezone` only when the stored values follow a different convention.
This setting does not apply to PostgreSQL, SQL Server, or SQLite sources.

Sources may share one Keychain account only when they intentionally use the same
database type, host, port, username, and password. Different database names are
allowed. The generator rejects unsafe sharing across different connections.

Resolve one stable Keychain service for the project. The default is
`codex.dbhub.<project-directory-name>`; use `--keychain-service` when that name
could collide with another project.

Never ask the user to send a password in chat. Never put a password in a command
argument. The user enters it only into the hidden prompt produced by
`security ... -w` when `-w` is the final option.

If production is requested, recommend a database-level read-only account. Keep
a user-specified account, but state that `readonly=true` is only an
application-layer safety net.

## Workflow

> [!IMPORTANT]
> Qualify the exact target version globally before generating or upgrading a
> project launcher. Reuse that credential; do not repeat the Docker matrix for
> each project.

1. Inspect before writing:
   - `git status --short`
   - existing `.codex/config.toml`
   - existing `.codex/dbhub/`
   - `git rev-parse --git-path info/exclude`
   - current `codex mcp get dbhub`, when available
   - `command -v node`, `command -v python3`, and `uname -s`
2. Resolve source fields, credential grouping, and the Keychain service.
3. Prepare one `--source` per database:

   ```text
   id|type|host|port|database|user|DISPLAY LABEL|KEYCHAIN ACCOUNT
   ```

4. Run the generator with `--dry-run`. Inspect:
   - all source endpoints and usernames;
   - each source's `keychain_account`;
   - `keychain.service`;
   - grouped sources under `keychain.credentials`;
   - the safe `check` and `create_or_update` commands;
   - `replacement_required`.
5. On macOS, check every Keychain item without reading its password:

   ```bash
   /usr/bin/security find-generic-password \
     -a '<KEYCHAIN ACCOUNT>' \
     -s '<KEYCHAIN SERVICE>' >/dev/null
   ```

   Use the command exit code only. Do not add `-w` or `-g` to an existence
   check because that would emit the secret.
6. If any item is missing, stop before writing the enabled MCP configuration.
   Give the user the corresponding `create_or_update` command from the dry-run
   summary:

   ```bash
   /usr/bin/security add-generic-password \
     -U \
     -a '<KEYCHAIN ACCOUNT>' \
     -s '<KEYCHAIN SERVICE>' \
     -l 'DBHub <KEYCHAIN ACCOUNT>' \
     -w
   ```

   `-w` must be the final option so `security` prompts without putting the
   password in shell history or process arguments. Ask the user to run it in
   their terminal and report completion; never ask them to paste the password.
   Re-run the password-free existence checks afterward and require exit code
   `0` for every account.
7. Inspect existing generated targets. Use `--force` only when replacement is
   intended, then run the generator for real.
8. Validate without database access:
   - `zsh -n .codex/dbhub/start-dbhub.zsh`
   - confirm project files contain only `${DBHUB_*_PASSWORD}` placeholders
   - set all generated password variables to dummy values and run
     `DBHUB_LAUNCHER_CHECK_ONLY=1 .codex/dbhub/start-dbhub.zsh`
   - start DBHub with dummy variables and stdin EOF; every source must be `lazy`
   - for one source, confirm tools are `execute_sql` and `search_objects`
   - for multiple sources, confirm tools are `execute_sql_<source>` and
     `search_objects_<source>`
   - confirm `git check-ignore -v` reports the generated files
   - confirm modes are `600` for TOML and `700` for the launcher
9. Verify Keychain integration without printing secrets:
   - unset generated password variables
   - run `DBHUB_LAUNCHER_CHECK_ONLY=1 .codex/dbhub/start-dbhub.zsh`
   - request the needed local Keychain permission when the execution
     environment requires it
   - do not run the full DBHub process with real credentials merely to validate
     launcher wiring
10. Report configured sources, Keychain service/accounts, exact checks and
    results, whether real authentication was tested, and the remaining
    database-account permission risk.

## Generator

Use the bundled script:

```bash
python3 <dbhub-setup-skill-dir>/scripts/setup_dbhub_project.py \
  --project-root /absolute/path/to/project \
  --dbhub-version '<qualified-version>' \
  --source 'prod_app|mysql|prod-db.example.com|3306|app|readonly_user|PROD app|prod' \
  --source 'prod_identity|mysql|prod-db.example.com|3306|identity|readonly_user|PROD identity|prod' \
  --source 'uat_app|mysql|uat-db.example.com|3306|app|uat_user|UAT app|uat' \
  --keychain-service 'codex.dbhub.my-project' \
  --dry-run
```

Remove `--dry-run` only after all Keychain existence checks pass. Add `--force`
only after inspecting an existing setup and confirming replacement is intended.

Useful options:

- `--keychain-service`: defaults to `codex.dbhub.<project-directory-name>`.
- `--dbhub-version`: required exact stable version from a reusable
  qualification credential; never infer a target from a stale project default.
  The launcher fails closed when that credential/runtime is absent.
- `--npm-registry`: rejected for project launchers; use the global release
  manager's `--registry` option only for qualification.
- `--max-rows`: defaults to `1000`.
- `--connection-timeout`: defaults to `10` seconds.
- `--query-timeout`: defaults to `30` seconds.
- `--mysql-timezone`: defaults to `+08:00` for generated MySQL/MariaDB sources;
  accepts `local`, `Z`, or an offset such as `+09:00`.
- `--no-git-exclude`: do not update the repository-local Git exclude file.

The eighth `--source` field is optional for backward compatibility. When
omitted, the source ID becomes its Keychain account, so explicitly provide it
when multiple databases share one credential.

## Generated Contract

- Project MCP override: `.codex/config.toml`.
- DBHub sources/tools: `.codex/dbhub/dbhub.toml`.
- Keychain launcher: `.codex/dbhub/start-dbhub.zsh`.
- Generated launcher schema marker: `dbhub-setup-launcher-schema: 2`.
- Password environment variable: `DBHUB_<NORMALIZED_SOURCE_ID>_PASSWORD`.
- Project files contain no literal password.
- On macOS, the launcher reads each credential group from the configured
  Keychain service and supplies it to all grouped source variables.
- Pre-set password environment variables override Keychain lookup.
- Normal startup validates the fixed runner, qualification credential/runtime,
  and bound TOML hash before reading Keychain. The
  `DBHUB_LAUNCHER_CHECK_ONLY=1` branch is only a Keychain wiring diagnostic and
  intentionally skips runtime qualification; use dummy password variables for
  file-only tests.
- A missing Keychain item produces a safe creation command and exits; it does
  not display repeated custom password dialogs.
- On non-macOS systems, missing environment variables fail closed because
  macOS Keychain is unavailable.
- Passwords are persisted only in macOS Keychain, then exist in the DBHub child
  process environment while that process runs.
- The launcher executes the locally qualified runtime whose lockfile was tested;
  it does not resolve DBHub or transitive dependencies with `npx` at startup.
- The launcher does not accept environment overrides for its runner, state
  directory, config path, Keychain service, or Python interpreter.
- Every source has `lazy = true`.
- Every generated MySQL/MariaDB source has `timezone = "+08:00"` unless the
  generator is explicitly given another valid `--mysql-timezone` value.
- Every `execute_sql` has `readonly = true` and bounded `max_rows`.
- Project MCP configuration sets `args = []` so global DBHub arguments cannot
  leak through Codex config merging.
- Codex may start enabled MCP servers when opening or switching tasks. This
  launcher can therefore read Keychain credentials before a DBHub tool is used;
  `lazy` defers database connection, not MCP startup or Keychain lookup.

## Safety Stops

- Stop if a password, token, or secret would be printed, logged, written to a
  project/shell file, passed as a command argument, or sent through chat.
- Never use `security add-generic-password -A`; it allows any application to
  access the item without warning.
- Do not run `security find-generic-password -w` or `-g` directly where stdout
  is captured or displayed. Let the launcher capture it internally.
- Stop before writing a new enabled MCP setup when required Keychain items are
  missing; provide the safe creation commands first.
- Stop before replacing an existing non-generated DBHub setup unless the user
  confirms the intended replacement.
- Stop before a production connection test unless private-system access is
  explicitly approved.
- Do not claim successful login from Keychain existence, launcher checks, TOML
  parsing, tool registration, lazy startup, or MCP initialization.
- Do not stage or commit `.codex/dbhub/` connection details unless the user
  explicitly chooses a repository-sharing model and confirms the non-secret
  internal endpoints are safe to commit.
