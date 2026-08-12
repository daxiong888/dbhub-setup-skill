#!/usr/bin/env python3
"""Exec the immutable runtime recorded by a DBHub qualification credential."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


PACKAGE_NAME = "@bytebase/dbhub"
QUALIFICATION_SCHEMA_VERSION = 2
QUALIFICATION_POLICY_VERSION = 3
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
REUSABLE_STATUSES = {"qualified", "partially_qualified"}
ALLOWED_CONNECTORS = {"mariadb", "mysql", "postgres", "sqlite", "sqlserver"}
DEFAULT_STATE_DIR = Path.home() / ".codex" / "state" / "dbhub-setup"
SCRIPT_DIR = Path(__file__).resolve().parent
MATRIX_PATH = SCRIPT_DIR / "dbhub_release_matrix.json"
QUALIFIER_PATH = SCRIPT_DIR / "qualify_dbhub_release.mjs"
HARNESS_DIR = SCRIPT_DIR.parent / "qualification"


class RunnerError(RuntimeError):
    """A safe qualified-runtime resolution failure."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_tree(root: Path) -> str:
    if not root.is_dir():
        raise RunnerError(f"runtime root is not a directory: {root}")
    digest = hashlib.sha256()
    resolved_root = root.resolve()
    root_info = root.stat()
    digest.update(b"root\0")
    digest.update(f"{stat.S_IMODE(root_info.st_mode):o}".encode("ascii"))
    digest.update(b"\0")

    def visit(directory: Path, relative_parent: Path) -> None:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: os.fsencode(item.name))
        for entry in entries:
            relative = relative_parent / entry.name
            info = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                kind = b"d"
                payload_size = 0
            elif stat.S_ISREG(info.st_mode):
                kind = b"f"
                payload_size = info.st_size
            elif stat.S_ISLNK(info.st_mode):
                kind = b"l"
                link_text = os.readlink(entry.path)
                if os.path.isabs(link_text):
                    raise RunnerError(
                        f"runtime contains an absolute symlink: {relative.as_posix()}"
                    )
                try:
                    link_target = (
                        Path(entry.path).parent / link_text
                    ).resolve(strict=True)
                    link_target.relative_to(resolved_root)
                except (OSError, RuntimeError, ValueError) as error:
                    raise RunnerError(
                        f"runtime symlink escapes or is broken: {relative.as_posix()}"
                    ) from error
                payload = os.fsencode(link_text)
                payload_size = len(payload)
            else:
                raise RunnerError(
                    f"runtime contains unsupported file type: {relative.as_posix()}"
                )
            digest.update(kind)
            digest.update(b"\0")
            digest.update(f"{mode:o}".encode("ascii"))
            digest.update(b"\0")
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(payload_size.to_bytes(8, "big"))
            if kind == b"f":
                with open(entry.path, "rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
            elif kind == b"l":
                digest.update(payload)
            if kind == b"d":
                visit(Path(entry.path), relative)

    visit(root, Path())
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def current_contract_inputs() -> dict[str, Any]:
    harness_package = json.loads(
        (HARNESS_DIR / "package.json").read_text(encoding="utf-8")
    )
    dependencies = harness_package.get("dependencies")
    if not isinstance(dependencies, dict):
        raise RunnerError("qualification harness dependencies are missing")
    harness_lock = json.loads(
        (HARNESS_DIR / "package-lock.json").read_text(encoding="utf-8")
    )
    packages = harness_lock.get("packages")
    core = (
        packages.get("node_modules/testcontainers")
        if isinstance(packages, dict)
        else None
    )
    if not isinstance(core, dict) or not isinstance(core.get("version"), str):
        raise RunnerError("qualification harness lock is missing testcontainers core")
    return {
        "node_major": node_major(),
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "mcp_client_version": dependencies.get("@modelcontextprotocol/client"),
        "testcontainers_adapter_version": dependencies.get("@testcontainers/mysql"),
        "testcontainers_core_version": core["version"],
        "harness_lock_sha256": sha256_file(HARNESS_DIR / "package-lock.json"),
        "qualification_policy_version": QUALIFICATION_POLICY_VERSION,
        "matrix_sha256": sha256_file(MATRIX_PATH),
        "qualifier_sha256": sha256_file(QUALIFIER_PATH),
    }


def validate_credential_inputs(credential: dict[str, Any]) -> None:
    if credential.get("schema_version") != QUALIFICATION_SCHEMA_VERSION:
        raise RunnerError("qualification credential schema is not reusable")
    inputs = credential.get("inputs")
    fingerprint = credential.get("input_fingerprint")
    if not isinstance(inputs, dict) or not isinstance(fingerprint, str):
        raise RunnerError("qualification credential is missing its input binding")
    if canonical_hash(inputs) != fingerprint:
        raise RunnerError("qualification credential input fingerprint mismatch")
    candidate = credential.get("candidate")
    if not isinstance(candidate, dict):
        raise RunnerError("qualification credential is missing candidate metadata")
    identity_mismatches = [
        name
        for name in ("package", "version", "integrity")
        if inputs.get(name)
        != (
            candidate.get("name")
            if name == "package"
            else candidate.get(name)
        )
    ]
    if identity_mismatches:
        raise RunnerError(
            "qualification credential candidate binding mismatch: "
            + ", ".join(identity_mismatches)
        )
    expected = current_contract_inputs()
    mismatches = [
        name for name, value in expected.items() if inputs.get(name) != value
    ]
    if mismatches:
        raise RunnerError(
            "qualification credential is stale for: " + ", ".join(mismatches)
        )


def node_major() -> int:
    result = subprocess.run(
        ["node", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    match = re.fullmatch(r"v?(\d+)\.\d+\.\d+\s*", result.stdout)
    if result.returncode != 0 or not match:
        raise RunnerError("unable to determine Node.js major version")
    return int(match.group(1))


def load_credential(state_dir: Path, version: str) -> tuple[Path, dict[str, Any]]:
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in (state_dir / "qualifications").glob(f"{version}-*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("status") in REUSABLE_STATUSES:
            matches.append((path, value))
    if len(matches) != 1:
        raise RunnerError(
            f"expected one qualified runtime for DBHub {version}, found {len(matches)}"
        )
    return matches[0]


def resolve_qualified_runtime(
    state_dir: Path,
    version: str,
) -> tuple[Path, dict[str, Any], Path]:
    credential_path, credential = load_credential(state_dir, version)
    if credential.get("candidate", {}).get("name") != PACKAGE_NAME:
        raise RunnerError("qualification credential package name mismatch")
    if credential.get("candidate", {}).get("version") != version:
        raise RunnerError("qualification credential version mismatch")
    validate_credential_inputs(credential)
    return credential_path, credential, validate_runtime(
        credential,
        version,
        state_dir=state_dir,
    )


def resolve_entry(
    state_dir: Path,
    version: str,
) -> tuple[Path, Path]:
    credential_path, _, entry = resolve_qualified_runtime(state_dir, version)
    return credential_path, entry


def validate_runtime(
    credential: dict[str, Any],
    version: str,
    *,
    state_dir: Path,
) -> Path:
    runtime = credential.get("runtime")
    if not isinstance(runtime, dict):
        raise RunnerError("qualification credential is missing runtime metadata")
    root = Path(str(runtime.get("root", "")))
    entry = Path(str(runtime.get("entry", "")))
    lock = root / "package-lock.json"
    package_json = root / "node_modules/@bytebase/dbhub/package.json"
    if not root.is_absolute() or not entry.is_absolute():
        raise RunnerError("qualified runtime paths must be absolute")
    try:
        root.resolve().relative_to((state_dir / "runtimes").resolve())
    except (OSError, ValueError) as error:
        raise RunnerError(
            "qualified runtime root is outside the qualification state"
        ) from error
    expected_entry = root / "node_modules/@bytebase/dbhub/dist/index.js"
    if entry != expected_entry:
        raise RunnerError("qualified runtime entry path is not canonical")
    if not lock.is_file() or not entry.is_file() or not package_json.is_file():
        raise RunnerError("qualified runtime files are missing")
    if sha256_file(lock) != runtime.get("package_lock_sha256"):
        raise RunnerError("qualified runtime package-lock.json has changed")
    package = json.loads(package_json.read_text(encoding="utf-8"))
    if package.get("name") != PACKAGE_NAME or package.get("version") != version:
        raise RunnerError("qualified runtime package identity mismatch")
    expected_tree = runtime.get("runtime_tree_sha256")
    if (
        runtime.get("runtime_tree_hash_version") != 1
        or not isinstance(expected_tree, str)
    ):
        raise RunnerError("qualified runtime tree binding is missing")
    if sha256_tree(root) != expected_tree:
        raise RunnerError("qualified runtime content tree has changed")
    return entry


def qualified_connectors(credential: dict[str, Any]) -> set[str]:
    matrix = credential.get("matrix")
    if not isinstance(matrix, dict):
        return set()
    summary = matrix.get("connector_summary")
    if isinstance(summary, dict):
        return {
            connector
            for connector, value in summary.items()
            if isinstance(connector, str)
            and isinstance(value, dict)
            and value.get("status") == "pass"
        }
    return set()


def config_path_from_args(dbhub_args: list[str]) -> Path:
    matches: list[Path] = []
    for index, value in enumerate(dbhub_args):
        if value.startswith("--config="):
            matches.append(Path(value.split("=", 1)[1]).expanduser().absolute())
        if value == "--config":
            if index + 1 >= len(dbhub_args):
                raise RunnerError("--config requires a path")
            matches.append(Path(dbhub_args[index + 1]).expanduser().absolute())
    if len(matches) != 1:
        raise RunnerError(
            f"qualified project launcher requires exactly one --config, found {len(matches)}"
        )
    return matches[0]


def positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def expected_password_placeholder(source_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "_", source_id).upper()
    return f"${{DBHUB_{normalized}_PASSWORD}}"


def validate_config_file(
    credential: dict[str, Any],
    config_path: Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        config_info = config_path.lstat()
    except FileNotFoundError as error:
        raise RunnerError("DBHub config is missing") from error
    if (
        not stat.S_ISREG(config_info.st_mode)
        or config_info.st_uid != os.geteuid()
    ):
        raise RunnerError("DBHub config must be an owner-controlled regular file")
    if not config_path.is_file():
        raise RunnerError("DBHub config is missing")
    if stat.S_IMODE(config_info.st_mode) != 0o600:
        raise RunnerError("DBHub config mode must be 600")
    if expected_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise RunnerError("expected DBHub config hash is invalid")
        if sha256_file(config_path) != expected_sha256:
            raise RunnerError("DBHub config content hash has changed")
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    if set(parsed) != {"sources", "tools"}:
        raise RunnerError("DBHub config must contain only generated sources and tools")
    sources = parsed.get("sources")
    tools = parsed.get("tools")
    if not isinstance(sources, list) or not sources:
        raise RunnerError("DBHub config has no sources")
    if not isinstance(tools, list):
        raise RunnerError("DBHub config tools are missing")

    source_ids: set[str] = set()
    connectors: set[str] = set()
    common_source_keys = {
        "id",
        "description",
        "type",
        "host",
        "port",
        "database",
        "user",
        "password",
        "connection_timeout",
        "query_timeout",
        "lazy",
    }
    for source in sources:
        if not isinstance(source, dict):
            raise RunnerError("DBHub config contains an invalid source")
        source_id = source.get("id")
        connector = source.get("type")
        if (
            not isinstance(source_id, str)
            or not SOURCE_ID_PATTERN.fullmatch(source_id)
        ):
            raise RunnerError("DBHub config contains an invalid source id")
        if source_id in source_ids:
            raise RunnerError(f"DBHub config contains duplicate source id: {source_id}")
        source_ids.add(source_id)
        if connector not in ALLOWED_CONNECTORS:
            raise RunnerError(
                f"DBHub config contains unsupported connector: {connector!r}"
            )
        connectors.add(connector)
        expected_keys = set(common_source_keys)
        if connector in {"mysql", "mariadb"}:
            expected_keys.add("timezone")
        if set(source) != expected_keys:
            raise RunnerError(
                f"source {source_id} does not match the generated source contract"
            )
        for field in ("description", "host", "database", "user"):
            if not isinstance(source.get(field), str) or not source[field]:
                raise RunnerError(f"source {source_id} has an invalid {field}")
        if (
            not positive_integer(source.get("port"))
            or source["port"] > 65535
        ):
            raise RunnerError(f"source {source_id} has an invalid port")
        if (
            not positive_integer(source.get("connection_timeout"))
            or not positive_integer(source.get("query_timeout"))
        ):
            raise RunnerError(f"source {source_id} has invalid timeouts")
        if source.get("lazy") is not True:
            raise RunnerError(f"source {source_id} must keep lazy = true")
        if source.get("password") != expected_password_placeholder(source_id):
            raise RunnerError(
                f"source {source_id} password must use its generated environment placeholder"
            )
        if connector in {"mysql", "mariadb"}:
            timezone = source.get("timezone")
            if not isinstance(timezone, str) or not timezone:
                raise RunnerError(f"source {source_id} has an invalid timezone")

    seen_tools: set[tuple[str, str]] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise RunnerError("DBHub config contains an invalid tool")
        name = tool.get("name")
        source_id = tool.get("source")
        if name not in {"execute_sql", "search_objects"}:
            raise RunnerError(f"DBHub config contains unsupported tool: {name!r}")
        if source_id not in source_ids:
            raise RunnerError(
                f"DBHub tool {name} refers to an unknown source: {source_id!r}"
            )
        identity = (source_id, name)
        if identity in seen_tools:
            raise RunnerError(
                f"DBHub config contains duplicate {name} for source {source_id}"
            )
        seen_tools.add(identity)
        if name == "execute_sql":
            if set(tool) != {"name", "source", "readonly", "max_rows"}:
                raise RunnerError(
                    f"execute_sql for source {source_id} does not match the generated contract"
                )
            if tool.get("readonly") is not True:
                raise RunnerError(
                    f"execute_sql for source {source_id} must keep readonly = true"
                )
            if not positive_integer(tool.get("max_rows")):
                raise RunnerError(
                    f"execute_sql for source {source_id} has invalid max_rows"
                )
        elif set(tool) != {"name", "source"}:
            raise RunnerError(
                f"search_objects for source {source_id} does not match the generated contract"
            )

    expected_tools = {
        (source_id, name)
        for source_id in source_ids
        for name in ("execute_sql", "search_objects")
    }
    if seen_tools != expected_tools:
        raise RunnerError(
            "DBHub config must have exactly execute_sql and search_objects per source"
        )

    covered = qualified_connectors(credential)
    missing = sorted(connectors - covered)
    if missing:
        raise RunnerError(
            "qualified runtime does not cover project connectors: "
            + ", ".join(missing)
        )
    return parsed


def validate_config_coverage(
    credential: dict[str, Any],
    dbhub_args: list[str],
) -> None:
    config_path = config_path_from_args(dbhub_args)
    validate_config_file(credential, config_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an immutable, locally qualified DBHub runtime."
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--validate-config", type=Path)
    parser.add_argument("dbhub_args", nargs=argparse.REMAINDER)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not SEMVER_PATTERN.fullmatch(args.version):
        raise RunnerError("--version must be an exact stable semver")
    state_dir = args.state_dir.expanduser().resolve()
    _, credential, entry = resolve_qualified_runtime(state_dir, args.version)
    if args.validate_config is not None:
        if args.dbhub_args:
            raise RunnerError("--validate-config cannot be combined with DBHub arguments")
        config_path = args.validate_config.expanduser().absolute()
        validate_config_file(
            credential,
            config_path,
            expected_sha256=args.expected_config_sha256,
        )
        return 0
    dbhub_args = list(args.dbhub_args)
    if dbhub_args and dbhub_args[0] == "--":
        dbhub_args.pop(0)
    config_path = config_path_from_args(dbhub_args)
    validate_config_file(
        credential,
        config_path,
        expected_sha256=args.expected_config_sha256,
    )
    os.execvpe(
        "node",
        ["node", str(entry), *dbhub_args],
        os.environ.copy(),
    )
    raise AssertionError("os.execvpe returned unexpectedly")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        RunnerError,
        OSError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as error:
        print(f"DBHub qualified runtime error: {error}", file=sys.stderr)
        raise SystemExit(1)
