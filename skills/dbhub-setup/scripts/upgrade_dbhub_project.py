#!/usr/bin/env python3
"""Apply an already-qualified exact DBHub version to one generated project."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any


PACKAGE_TOKEN_PATTERN = re.compile(
    r"@bytebase/dbhub@(?P<version>\d+\.\d+\.\d+)"
)
RUNNER_VERSION_PATTERN = re.compile(
    r"(?m)^\s*--version\s+(?P<version>\d+\.\d+\.\d+)\s*\\$"
)
NPX_TAIL_MARKER = "\nexec npx \\\n"
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
KEYCHAIN_SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
KEYCHAIN_ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
PASSWORD_ENV_PATTERN = re.compile(r"^DBHUB_[A-Z0-9_]+_PASSWORD$")
LAUNCHER_SCHEMA_MARKER = "# dbhub-setup-launcher-schema: 2"
LOAD_BLOCK_PATTERN = re.compile(
    r"(?m)^[ \t]*load_keychain_password \\\n(?:[ \t]+[^\n]+ \\\n)*[ \t]+[^\n]+\s*$"
)
REUSABLE_STATUSES = {"qualified", "partially_qualified"}
DEFAULT_STATE_DIR = Path.home() / ".codex" / "state" / "dbhub-setup"
SCRIPT_DIR = Path(__file__).resolve().parent
QUALIFIED_RUNNER_PATH = SCRIPT_DIR / "run_qualified_dbhub.py"
PYTHON_EXECUTABLE = Path(shutil.which("python3") or os.sys.executable).resolve()


def load_qualified_runner():
    path = SCRIPT_DIR / "run_qualified_dbhub.py"
    spec = importlib.util.spec_from_file_location(
        "dbhub_setup_qualified_runner",
        path,
    )
    if spec is None or spec.loader is None:
        raise UpgradeError("unable to load qualified runtime validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UpgradeError(RuntimeError):
    """A safe project-upgrade failure."""


def read_qualification(
    state_dir: Path,
    target: str,
) -> tuple[Path, dict[str, Any]]:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in (state_dir / "qualifications").glob(f"{target}-*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("status") in REUSABLE_STATUSES:
            candidate = value.get("candidate")
            if (
                not isinstance(candidate, dict)
                or candidate.get("name") != "@bytebase/dbhub"
                or candidate.get("version") != target
            ):
                raise UpgradeError("qualification credential package identity mismatch")
            runner = load_qualified_runner()
            try:
                runner.validate_credential_inputs(value)
                runner.validate_runtime(value, target, state_dir=state_dir)
            except runner.RunnerError as error:
                raise UpgradeError(
                    f"qualification credential is not reusable: {error}"
                ) from error
            candidates.append((path, value))
    if len(candidates) != 1:
        raise UpgradeError(
            f"expected exactly one valid qualification for {target}, "
            f"found {len(candidates)}"
        )
    return candidates[0]


def qualified_connectors(qualification: dict[str, Any]) -> set[str]:
    summary = qualification.get("matrix", {}).get("connector_summary", {})
    if not isinstance(summary, dict):
        return set()
    return {
        connector
        for connector, value in summary.items()
        if isinstance(connector, str)
        and isinstance(value, dict)
        and value.get("status") == "pass"
    }


def atomic_write(path: Path, content: str, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def extract_keychain_service(original: str) -> str:
    match = re.search(r"(?m)^readonly KEYCHAIN_SERVICE=(?P<value>.+)$", original)
    if match is None:
        raise UpgradeError("generated launcher Keychain binding is missing")
    value = match.group("value").strip()
    dynamic = re.fullmatch(
        r'"\$\{DBHUB_KEYCHAIN_SERVICE:-(?P<service>[A-Za-z0-9._-]+)\}"',
        value,
    )
    if dynamic is not None:
        service = dynamic.group("service")
    else:
        try:
            tokens = shlex.split(value)
        except ValueError as error:
            raise UpgradeError("generated launcher Keychain binding is invalid") from error
        if len(tokens) != 1:
            raise UpgradeError("generated launcher Keychain binding is invalid")
        service = tokens[0]
    if not KEYCHAIN_SERVICE_PATTERN.fullmatch(service):
        raise UpgradeError("generated launcher Keychain service is invalid")
    return service


def extract_keychain_loads(original: str) -> list[tuple[str, list[str]]]:
    loads: list[tuple[str, list[str]]] = []
    for match in LOAD_BLOCK_PATTERN.finditer(original):
        flattened = match.group(0).replace("\\\n", " ")
        try:
            tokens = shlex.split(flattened)
        except ValueError as error:
            raise UpgradeError("generated launcher Keychain load is invalid") from error
        if (
            len(tokens) < 3
            or tokens[0] != "load_keychain_password"
            or not KEYCHAIN_ACCOUNT_PATTERN.fullmatch(tokens[1])
            or any(not PASSWORD_ENV_PATTERN.fullmatch(item) for item in tokens[2:])
        ):
            raise UpgradeError("generated launcher Keychain load is invalid")
        loads.append((tokens[1], tokens[2:]))
    if not loads:
        raise UpgradeError("generated launcher has no recognized Keychain loads")
    flattened_envs = [item for _, values in loads for item in values]
    if len(flattened_envs) != len(set(flattened_envs)):
        raise UpgradeError("generated launcher has duplicate password environment loads")
    return loads


def render_hardened_launcher(
    *,
    target: str,
    keychain_service: str,
    loads: list[tuple[str, list[str]]],
    config_sha256: str,
    qualification_state_dir: Path,
) -> str:
    load_blocks = []
    for account, password_envs in loads:
        arguments = [account, *password_envs]
        continuation = " \\\n  ".join(shlex.quote(value) for value in arguments)
        block = f"load_keychain_password \\\n  {continuation}"
        load_blocks.append(
            "\n".join(f"  {line}" for line in block.splitlines())
        )
    keychain_loads = "\n\n".join(load_blocks)
    return f"""#!/bin/zsh
{LAUNCHER_SCHEMA_MARKER}

set -eu

readonly SCRIPT_DIR="${{0:A:h}}"
readonly CONFIG_PATH="${{SCRIPT_DIR}}/dbhub.toml"
readonly KEYCHAIN_SERVICE={shlex.quote(keychain_service)}
readonly QUALIFIED_RUNNER={shlex.quote(str(QUALIFIED_RUNNER_PATH))}
readonly QUALIFICATION_STATE_DIR={shlex.quote(str(qualification_state_dir))}
readonly PYTHON_EXECUTABLE={shlex.quote(str(PYTHON_EXECUTABLE))}

if [[ ! -r "${{CONFIG_PATH}}" ]]; then
  print -u2 -- "DBHub config is not readable: ${{CONFIG_PATH}}"
  exit 1
fi

load_keychain_password() {{
  local keychain_account="$1"
  local password_env_name
  local keychain_password
  local password_missing=0
  shift

  for password_env_name in "$@"; do
    if [[ -z "${{(P)password_env_name:-}}" ]]; then
      password_missing=1
      break
    fi
  done

  if (( password_missing == 0 )); then
    return 0
  fi

  if [[ "$(/usr/bin/uname -s)" != "Darwin" ]]; then
    print -u2 -- "DBHub password '${{keychain_account}}' is missing and macOS Keychain is unavailable."
    return 1
  fi

  keychain_password="$(
    /usr/bin/security find-generic-password \\
      -a "${{keychain_account}}" \\
      -s "${{KEYCHAIN_SERVICE}}" \\
      -w 2>/dev/null
  )" || {{
    print -u2 -- "DBHub could not read account '${{keychain_account}}' from macOS Keychain."
    print -u2 -- "Create it in a terminal with:"
    print -u2 -- "  /usr/bin/security add-generic-password -U -a '${{keychain_account}}' -s '${{KEYCHAIN_SERVICE}}' -l 'DBHub ${{keychain_account}}' -w"
    return 1
  }}

  if [[ -z "${{keychain_password}}" ]]; then
    print -u2 -- "DBHub found an empty password for account '${{keychain_account}}' in macOS Keychain."
    return 1
  fi

  for password_env_name in "$@"; do
    if [[ -z "${{(P)password_env_name:-}}" ]]; then
      typeset -gx "${{password_env_name}}=${{keychain_password}}"
    fi
  done

  unset keychain_password password_env_name
}}

load_all_passwords() {{
{keychain_loads}
}}

if [[ "${{DBHUB_LAUNCHER_CHECK_ONLY:-0}}" == "1" ]]; then
  load_all_passwords
  print -- "DBHub launcher check passed: required passwords were loaded without printing them."
  exit 0
fi

if [[ ! -r "${{QUALIFIED_RUNNER}}" ]]; then
  print -u2 -- "DBHub qualified runtime runner is not readable: ${{QUALIFIED_RUNNER}}"
  exit 1
fi

# DBHUB_QUALIFIED_PREFLIGHT_BEGIN
"${{PYTHON_EXECUTABLE}}" "${{QUALIFIED_RUNNER}}" \\
  --version {target} \\
  --state-dir "${{QUALIFICATION_STATE_DIR}}" \\
  --expected-config-sha256 {config_sha256} \\
  --validate-config "${{CONFIG_PATH}}"
# DBHUB_QUALIFIED_PREFLIGHT_END

load_all_passwords

exec "${{PYTHON_EXECUTABLE}}" "${{QUALIFIED_RUNNER}}" \\
  --version {target} \\
  --state-dir "${{QUALIFICATION_STATE_DIR}}" \\
  --expected-config-sha256 {config_sha256} \\
  -- \\
  --transport stdio \\
  --config="${{CONFIG_PATH}}"
"""


def render_legacy_npx_launcher(
    *,
    version: str,
    keychain_service: str,
    loads: list[tuple[str, list[str]]],
) -> str:
    load_blocks = []
    for account, password_envs in loads:
        arguments = [account, *password_envs]
        continuation = " \\\n  ".join(shlex.quote(value) for value in arguments)
        load_blocks.append(f"load_keychain_password \\\n  {continuation}")
    keychain_loads = "\n\n".join(load_blocks)
    return f"""#!/bin/zsh

set -eu

readonly SCRIPT_DIR="${{0:A:h}}"
readonly CONFIG_PATH="${{DBHUB_CONFIG_PATH:-${{SCRIPT_DIR}}/dbhub.toml}}"
readonly KEYCHAIN_SERVICE="${{DBHUB_KEYCHAIN_SERVICE:-{keychain_service}}}"

if [[ ! -r "${{CONFIG_PATH}}" ]]; then
  print -u2 -- "DBHub config is not readable: ${{CONFIG_PATH}}"
  exit 1
fi

load_keychain_password() {{
  local keychain_account="$1"
  local password_env_name
  local keychain_password
  local password_missing=0
  shift

  for password_env_name in "$@"; do
    if [[ -z "${{(P)password_env_name:-}}" ]]; then
      password_missing=1
      break
    fi
  done

  if (( password_missing == 0 )); then
    return 0
  fi

  if [[ "$(/usr/bin/uname -s)" != "Darwin" ]]; then
    print -u2 -- "DBHub password '${{keychain_account}}' is missing and macOS Keychain is unavailable."
    return 1
  fi

  keychain_password="$(
    /usr/bin/security find-generic-password \\
      -a "${{keychain_account}}" \\
      -s "${{KEYCHAIN_SERVICE}}" \\
      -w 2>/dev/null
  )" || {{
    print -u2 -- "DBHub could not read account '${{keychain_account}}' from macOS Keychain."
    print -u2 -- "Create it in a terminal with:"
    print -u2 -- "  /usr/bin/security add-generic-password -U -a '${{keychain_account}}' -s '${{KEYCHAIN_SERVICE}}' -l 'DBHub ${{keychain_account}}' -w"
    return 1
  }}

  if [[ -z "${{keychain_password}}" ]]; then
    print -u2 -- "DBHub found an empty password for account '${{keychain_account}}' in macOS Keychain."
    return 1
  fi

  for password_env_name in "$@"; do
    if [[ -z "${{(P)password_env_name:-}}" ]]; then
      typeset -gx "${{password_env_name}}=${{keychain_password}}"
    fi
  done

  unset keychain_password password_env_name
}}

{keychain_loads}

if [[ "${{DBHUB_LAUNCHER_CHECK_ONLY:-0}}" == "1" ]]; then
  print -- "DBHub launcher check passed: required passwords were loaded without printing them."
  exit 0
fi

exec npx \\
  -y \\
  @bytebase/dbhub@{version} \\
  --transport stdio \\
  --config="${{CONFIG_PATH}}"
"""


def update_launcher(
    original: str,
    target: str,
    *,
    config_sha256: str,
    qualification_state_dir: Path,
) -> tuple[str, str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", config_sha256):
        raise UpgradeError("DBHub config hash is invalid")
    package_matches = list(PACKAGE_TOKEN_PATTERN.finditer(original))
    runner_matches = list(RUNNER_VERSION_PATTERN.finditer(original))
    if package_matches and runner_matches:
        raise UpgradeError("launcher mixes npx and qualified runtime modes")
    service = extract_keychain_service(original)
    loads = extract_keychain_loads(original)
    if len(package_matches) == 1 and NPX_TAIL_MARKER in original:
        current = package_matches[0].group("version")
        transition = "npx-to-qualified"
        expected_legacy = render_legacy_npx_launcher(
            version=current,
            keychain_service=service,
            loads=loads,
        )
        if original != expected_legacy:
            raise UpgradeError(
                "legacy launcher is not an exact generated template; regenerate it"
            )
    elif runner_matches and "run_qualified_dbhub.py" in original:
        versions = {match.group("version") for match in runner_matches}
        if len(versions) != 1:
            raise UpgradeError("launcher contains inconsistent qualified versions")
        current = versions.pop()
        transition = "qualified"
        if LAUNCHER_SCHEMA_MARKER not in original:
            raise UpgradeError(
                "qualified launcher has no supported schema marker; regenerate it"
            )
        expected_current = render_hardened_launcher(
            target=current,
            keychain_service=service,
            loads=loads,
            config_sha256=config_sha256,
            qualification_state_dir=qualification_state_dir,
        )
        if original != expected_current:
            raise UpgradeError(
                "qualified launcher differs from its generated schema; regenerate it"
            )
    else:
        raise UpgradeError(
            "launcher is not a recognized generated npx or qualified-runtime launcher"
        )

    updated = render_hardened_launcher(
        target=target,
        keychain_service=service,
        loads=loads,
        config_sha256=config_sha256,
        qualification_state_dir=qualification_state_dir,
    )
    return updated, current, transition


def project_plan(
    *,
    project_root: Path,
    target: str,
    state_dir: Path,
    apply: bool,
    confirmed_keychain_mapping: str | None = None,
) -> dict[str, Any]:
    launcher = project_root / ".codex/dbhub/start-dbhub.zsh"
    config = project_root / ".codex/dbhub/dbhub.toml"
    if not launcher.is_file() or not config.is_file():
        raise UpgradeError("generated DBHub launcher or TOML is missing")

    launcher_mode = stat.S_IMODE(launcher.stat().st_mode)
    config_mode = stat.S_IMODE(config.stat().st_mode)
    if launcher_mode != 0o700 or config_mode != 0o600:
        raise UpgradeError(
            f"unexpected file modes: launcher={launcher_mode:o}, config={config_mode:o}"
        )

    original = launcher.read_text(encoding="utf-8")
    qualification_path, qualification = read_qualification(state_dir, target)
    runner = load_qualified_runner()
    try:
        parsed = runner.validate_config_file(qualification, config)
    except runner.RunnerError as error:
        raise UpgradeError(f"project DBHub config is not reusable: {error}") from error
    config_sha256 = runner.sha256_file(config)
    updated, current, runtime_transition = update_launcher(
        original,
        target,
        config_sha256=config_sha256,
        qualification_state_dir=state_dir,
    )
    sources = parsed.get("sources")
    source_summary: list[dict[str, str]] = []
    expected_password_envs: set[str] = set()
    for source in sources:
        source_id = source.get("id")
        connector = source.get("type")
        source_summary.append({"id": source_id, "type": connector})
        expected_password_envs.add(
            runner.expected_password_placeholder(source_id)[2:-1]
        )
    launcher_password_envs = {
        password_env
        for _, password_envs in extract_keychain_loads(updated)
        for password_env in password_envs
    }
    if launcher_password_envs != expected_password_envs:
        raise UpgradeError(
            "generated launcher password mappings do not match dbhub.toml sources"
        )
    keychain_mapping = [
        {
            "account": account,
            "password_envs": password_envs,
        }
        for account, password_envs in extract_keychain_loads(updated)
    ]
    keychain_mapping_sha256 = hashlib.sha256(
        json.dumps(
            keychain_mapping,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        apply
        and runtime_transition == "npx-to-qualified"
        and confirmed_keychain_mapping != keychain_mapping_sha256
    ):
        raise UpgradeError(
            "legacy Keychain mapping requires explicit confirmation with "
            f"--confirm-keychain-mapping {keychain_mapping_sha256}"
        )

    changed = updated != original
    if apply and changed:
        atomic_write(launcher, updated, launcher_mode)

    return {
        "mode": "apply" if apply else "plan",
        "project_root": str(project_root),
        "current_version": current,
        "target_version": target,
        "changed": changed,
        "runtime_transition": runtime_transition,
        "qualification_path": str(qualification_path),
        "qualification_scope": "connector-level; remote server versions not queried",
        "sources": source_summary,
        "keychain_mapping": keychain_mapping,
        "keychain_mapping_sha256": keychain_mapping_sha256,
        "keychain_mapping_confirmation_required": (
            runtime_transition == "npx-to-qualified"
        ),
        "real_database_tested": False,
        "next_checks": [
            "zsh syntax",
            "launcher check with dummy credentials",
            "fresh MCP initialize and tools/list",
            "optional per-source SELECT 1 only when explicitly authorized",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply a previously qualified exact DBHub version."
    )
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-keychain-mapping")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not SEMVER_PATTERN.fullmatch(args.target):
        raise UpgradeError("--target must be an exact stable semver")
    project_root = args.project_root.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    if not project_root.is_dir():
        raise UpgradeError(f"project root is not a directory: {project_root}")
    result = project_plan(
        project_root=project_root,
        target=args.target,
        state_dir=state_dir,
        apply=args.apply,
        confirmed_keychain_mapping=args.confirm_keychain_mapping,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UpgradeError, OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(1)
