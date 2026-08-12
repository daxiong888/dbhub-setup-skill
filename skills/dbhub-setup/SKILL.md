---
name: dbhub-setup
description: Set up project-level DBHub MCP for Codex on macOS, including multiple local, test, and production databases. Generate lazy read-only tools, keep passwords in macOS Keychain, and avoid plaintext credentials. Use when connecting a Codex project to DBHub or updating its database sources.
---

# DBHub Setup

Set up one project-level DBHub MCP server for multiple databases and environments.

This Skill is verified on macOS with OpenAI Codex and DBHub `1.2.0`. The bundled
generator uses that exact DBHub version by default. It does not run release
qualification or require Docker.

## Use this Skill when

- a Codex project needs DBHub MCP;
- one project needs LOCAL, UAT, PROD, or several databases at once;
- database passwords must stay out of project files and AI messages;
- an existing generated setup needs to be inspected or regenerated.

Do not use it to grant database permissions, create users, migrate data, or claim
that application-level read-only settings replace a database-level read-only
account.

## Safety contract

Always follow these rules:

1. Never ask the user to paste a database password into chat.
2. Never print, log, or store a password in project files, commands, or summaries.
3. Use a database account with the minimum required permissions, preferably a
   database-enforced read-only account.
4. Generate and inspect a dry-run before writing files.
5. Do not replace an existing DBHub MCP table or generated file without showing
   the conflict and receiving approval to use `--force`.
6. Do not connect to UAT, PROD, or another private database without explicit
   approval for that connection check.
7. Treat `readonly = true` as an application guardrail, not a complete security
   boundary.

## Locate the Skill

Resolve this installed Skill directory before running its script. Do not assume a
particular global installation path.

The generator is:

```text
<dbhub-setup-skill-dir>/scripts/setup_dbhub_project.py
```

## Collect non-secret inputs

For every database source, collect:

- source ID, such as `local_app`, `uat_app`, or `prod_reporting`;
- database type: `mysql`, `mariadb`, `postgres`, `sqlite`, or `sqlserver`;
- host and port;
- database name;
- username;
- display label;
- optional shared Keychain account ID.

The source format is:

```text
id|type|host|port|database|user[|DISPLAY LABEL[|KEYCHAIN ACCOUNT]]
```

Sources may share one Keychain account only when their database type, host, port,
and username are identical. This is useful when one login can access several
databases on the same server.

For MySQL and MariaDB, confirm the session timezone. The default is `+08:00`.
Accepted values are `local`, `Z`, or a fixed offset such as `+08:00`.

## Inspect the project first

Before generating anything:

1. inspect `git status --short --branch`;
2. inspect `.codex/config.toml` if present;
3. inspect `.codex/dbhub/` if present;
4. check whether `[mcp_servers.dbhub]` already exists;
5. keep unrelated project changes untouched.

## Generate a dry-run

Example:

```bash
python3 <dbhub-setup-skill-dir>/scripts/setup_dbhub_project.py \
  --project-root /absolute/path/to/project \
  --source 'local_app|mysql|127.0.0.1|3306|app|readonly_user|LOCAL app' \
  --source 'uat_app|mysql|uat.example.internal|3306|app|readonly_user|UAT app' \
  --source 'prod_reporting|postgres|prod.example.internal|5432|reporting|readonly_user|PROD reporting' \
  --mysql-timezone '+08:00' \
  --dry-run
```

The default is the exact verified version `1.2.0`. `--dbhub-version X.Y.Z` may be
used for another exact stable version, but the generated summary will mark it as
unverified. Do not use tags, ranges, prereleases, URLs, or arbitrary npm specs.

Review the dry-run summary for:

- the exact project root;
- source IDs, endpoints, usernames, and labels;
- Keychain service and account grouping;
- files that would be written;
- whether replacement is required;
- DBHub version and `dbhub_version_verified`.

## Prepare Keychain entries

The dry-run prints a `create_or_update` command for each Keychain account. Let the
user run that command in a local terminal and enter the password through the
hidden prompt. Do not run it with the password appended as an argument.

You may use the printed `check` command to confirm that an entry exists, but do
not add `-w`, because that would read the secret into command output.

The default Keychain service is derived from the project directory name. Set an
explicit `--keychain-service` when similarly named projects must remain separate.

## Write the setup

After the dry-run and Keychain entries are approved, rerun the same command
without `--dry-run`. Use `--force` only after the user approves replacing the
existing generated setup.

The generator writes:

```text
<project>/.codex/config.toml
<project>/.codex/dbhub/dbhub.toml
<project>/.codex/dbhub/start-dbhub.zsh
```

It also adds `.codex/` to the repository-local `.git/info/exclude` unless
`--no-git-exclude` is requested.

Generated behavior:

- passwords remain environment placeholders in `dbhub.toml`;
- the launcher loads missing passwords from macOS Keychain;
- each source uses `lazy = true`;
- `execute_sql` uses `readonly = true` and a finite `max_rows`;
- the launcher runs an exact `@bytebase/dbhub@X.Y.Z` package through `npx`;
- npm is fixed to the official registry and lifecycle scripts are disabled;
- the launcher refuses a DBHub config whose SHA-256 no longer matches the
  generated launcher.

The first real launch may need network access so `npx` can obtain the exact DBHub
package. Do not claim offline or immutable-runtime behavior.

## Verify without a real database

First inspect the generated files and permissions. Then validate shell syntax:

```bash
zsh -n <project>/.codex/dbhub/start-dbhub.zsh
```

For a file-only launcher check, provide dummy values for every generated password
environment variable and set `DBHUB_LAUNCHER_CHECK_ONLY=1`. This checks config
integrity and password loading without starting DBHub or connecting to a database.
Never use a real password for this check.

Only after explicit approval, restart Codex or reload the MCP configuration and
perform the smallest suitable real connection check. Report file generation,
launcher validation, MCP startup, and real database login as separate evidence.

## Report back

State:

- which project and source IDs were configured;
- the exact DBHub version and whether it is the verified default;
- which files were generated or replaced;
- whether Keychain entries were only checked or were created by the user;
- which checks passed;
- whether any real database was contacted;
- any unsupported platform, agent, or custom-version risk.
