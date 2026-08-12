#!/usr/bin/env python3
"""Check and qualify stable DBHub npm releases without touching projects."""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit


PACKAGE_NAME = "@bytebase/dbhub"
OFFICIAL_REGISTRY = "https://registry.npmjs.org"
QUALIFICATION_SCHEMA_VERSION = 2
QUALIFICATION_POLICY_VERSION = 3
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
REUSABLE_STATUSES = {"qualified", "partially_qualified"}
MINIMUM_ENGINE_PATTERN = re.compile(r"^>=\s*(\d+)\.(\d+)\.(\d+)$")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MATRIX = SCRIPT_DIR / "dbhub_release_matrix.json"
DEFAULT_QUALIFIER = SCRIPT_DIR / "qualify_dbhub_release.mjs"
DEFAULT_HARNESS_DIR = SCRIPT_DIR.parent / "qualification"
DEFAULT_STATE_DIR = Path.home() / ".codex" / "state" / "dbhub-setup"
SAFE_ENVIRONMENT_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "TMPDIR",
)


class QualificationError(RuntimeError):
    """A safe, user-facing qualification failure."""


class QualificationMatrixError(QualificationError):
    """A matrix completed but one or more required cells failed."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


class AlreadyRunningError(QualificationError):
    """Another live qualification process owns the global lock."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_tree(root: Path) -> str:
    """Hash every runtime entry without following symlinks."""
    if not root.is_dir():
        raise QualificationError(f"runtime root is not a directory: {root}")
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
            relative_bytes = relative.as_posix().encode("utf-8")
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
                    raise QualificationError(
                        f"runtime contains an absolute symlink: {relative.as_posix()}"
                    )
                try:
                    link_target = (
                        Path(entry.path).parent / link_text
                    ).resolve(strict=True)
                    link_target.relative_to(resolved_root)
                except (OSError, RuntimeError, ValueError) as error:
                    raise QualificationError(
                        f"runtime symlink escapes or is broken: {relative.as_posix()}"
                    ) from error
                payload = os.fsencode(link_text)
                payload_size = len(payload)
            else:
                raise QualificationError(
                    f"runtime contains unsupported file type: {relative.as_posix()}"
                )
            digest.update(kind)
            digest.update(b"\0")
            digest.update(f"{mode:o}".encode("ascii"))
            digest.update(b"\0")
            digest.update(relative_bytes)
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
    return sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def atomic_json_write(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def command_json(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> Any:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise QualificationError(f"{command[0]} failed: {message}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise QualificationError(
            f"{command[0]} returned invalid JSON"
        ) from error


def safe_environment(*, include_docker: bool = False) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in SAFE_ENVIRONMENT_KEYS
        if name in os.environ
    }
    environment.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    if include_docker:
        for name in ("DOCKER_CONFIG", "DOCKER_HOST"):
            if name in os.environ:
                environment[name] = os.environ[name]
    return environment


def validate_registry(registry: str) -> str:
    if registry != OFFICIAL_REGISTRY:
        raise QualificationError(
            f"--registry must be the official npm registry: {OFFICIAL_REGISTRY}"
        )
    return OFFICIAL_REGISTRY


def validate_official_npm_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "registry.npmjs.org"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise QualificationError(
            "npm artifact URL must use the official registry.npmjs.org origin"
        )


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise QualificationError(f"unsafe state directory: {path}")
    os.chmod(path, 0o700)


def atomic_empty_file(path: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise QualificationError(f"unsafe isolated npm config: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def npm_context(state_dir: Path) -> tuple[Path, list[str], dict[str, str]]:
    npm_root = state_dir / "npm"
    work_root = npm_root / "work"
    cache_root = npm_root / "cache"
    for directory in (state_dir, npm_root, work_root, cache_root):
        ensure_private_directory(directory)
    user_config = npm_root / "empty-user.npmrc"
    global_config = npm_root / "empty-global.npmrc"
    project_configs = (
        state_dir / ".npmrc",
        npm_root / ".npmrc",
        work_root / ".npmrc",
    )
    for config in (user_config, global_config, *project_configs):
        atomic_empty_file(config)
    options = [
        "--registry",
        OFFICIAL_REGISTRY,
        f"--@bytebase:registry={OFFICIAL_REGISTRY}",
        "--userconfig",
        str(user_config),
        "--globalconfig",
        str(global_config),
        "--cache",
        str(cache_root),
    ]
    return work_root, options, safe_environment()


def validate_lock_provenance(
    lock_path: Path,
    *,
    local_artifact_integrity: str | None = None,
) -> None:
    value = json.loads(lock_path.read_text(encoding="utf-8"))
    packages = value.get("packages")
    if not isinstance(packages, dict):
        raise QualificationError(f"npm lock packages are missing: {lock_path}")
    for package_path, package in packages.items():
        if not isinstance(package, dict):
            raise QualificationError(f"npm lock contains invalid package: {package_path}")
        resolved = package.get("resolved")
        if resolved is None:
            if package_path == "":
                continue
            raise QualificationError(
                f"npm lock package is missing resolved provenance: {package_path}"
            )
        integrity = package.get("integrity")
        if (
            local_artifact_integrity is not None
            and package_path == "node_modules/@bytebase/dbhub"
            and resolved == "file:artifact.tgz"
        ):
            if integrity != local_artifact_integrity:
                raise QualificationError(
                    "runtime lock DBHub integrity does not match the candidate"
                )
            continue
        if not isinstance(resolved, str):
            raise QualificationError(
                f"npm lock contains invalid resolved metadata: {package_path}"
            )
        validate_official_npm_url(resolved)
        if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
            raise QualificationError(
                f"npm lock package is missing sha512 integrity: {package_path}"
            )


def npm_json(
    arguments: list[str],
    *,
    state_dir: Path,
    cwd: Path | None = None,
    timeout: int = 300,
) -> Any:
    work_root, options, environment = npm_context(state_dir)
    execution_cwd = cwd or work_root
    try:
        execution_cwd.resolve().relative_to(work_root.resolve())
    except ValueError as error:
        raise QualificationError("npm working directory escaped isolated state") from error
    return command_json(
        ["npm", *arguments, *options],
        cwd=execution_cwd,
        env=environment,
        timeout=timeout,
    )


def npm_run(
    arguments: list[str],
    *,
    state_dir: Path,
    cwd: Path | None = None,
    timeout: int = 1200,
) -> subprocess.CompletedProcess[str]:
    work_root, options, environment = npm_context(state_dir)
    execution_cwd = cwd or work_root
    try:
        execution_cwd.resolve().relative_to(work_root.resolve())
    except ValueError as error:
        raise QualificationError("npm working directory escaped isolated state") from error
    return subprocess.run(
        ["npm", *arguments, *options],
        cwd=execution_cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def npm_metadata(
    version: str | None,
    *,
    registry: str,
    state_dir: Path,
) -> dict[str, Any]:
    spec = PACKAGE_NAME if version is None else f"{PACKAGE_NAME}@{version}"
    validate_registry(registry)
    raw = npm_json(
        [
        "view",
        spec,
        "name",
        "version",
        "engines",
        "dist.integrity",
        "dist.shasum",
        "dist.tarball",
        "--json",
        ],
        state_dir=state_dir,
    )
    if not isinstance(raw, dict):
        raise QualificationError("npm metadata is not an object")
    return raw


def metadata_field(metadata: dict[str, Any], dotted: str) -> Any:
    if dotted in metadata:
        return metadata[dotted]
    current: Any = metadata
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def parse_node_version(text: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if not match:
        raise QualificationError(f"unsupported Node.js version string: {text!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def current_node_version() -> str:
    result = subprocess.run(
        ["node", "--version"],
        env=safe_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise QualificationError("node --version failed")
    return result.stdout.strip().removeprefix("v")


def validate_metadata(
    metadata: dict[str, Any],
    *,
    node_version: str,
) -> dict[str, str]:
    name = metadata.get("name")
    version = metadata.get("version")
    engines = metadata.get("engines")
    integrity = metadata_field(metadata, "dist.integrity")
    shasum = metadata_field(metadata, "dist.shasum")
    tarball = metadata_field(metadata, "dist.tarball")

    if name != PACKAGE_NAME:
        raise QualificationError(f"unexpected npm package name: {name!r}")
    if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
        raise QualificationError(
            f"latest npm version is not an exact stable semver: {version!r}"
        )
    if not isinstance(engines, dict) or not isinstance(engines.get("node"), str):
        raise QualificationError("npm metadata is missing engines.node")
    engine = engines["node"]
    engine_match = MINIMUM_ENGINE_PATTERN.fullmatch(engine.strip())
    if not engine_match:
        raise QualificationError(f"unsupported engines.node expression: {engine!r}")
    required = tuple(int(part) for part in engine_match.groups())
    actual = parse_node_version(node_version)
    if actual < required:
        raise QualificationError(
            f"Node.js {node_version} does not satisfy {engine}"
        )
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise QualificationError("npm metadata is missing a sha512 dist.integrity")
    if not isinstance(shasum, str) or not re.fullmatch(r"[0-9a-f]{40}", shasum):
        raise QualificationError("npm metadata is missing a valid dist.shasum")
    if not isinstance(tarball, str):
        raise QualificationError("npm metadata is missing dist.tarball")
    validate_official_npm_url(tarball)

    return {
        "name": name,
        "version": version,
        "engine": engine,
        "integrity": integrity,
        "shasum": shasum,
        "tarball": tarball,
    }


def read_package_version(package_json: Path) -> str:
    value = json.loads(package_json.read_text(encoding="utf-8"))
    version = value.get("version")
    if not isinstance(version, str):
        raise QualificationError(f"missing version in {package_json}")
    return version


def qualification_inputs(
    *,
    candidate: dict[str, str],
    matrix_path: Path,
    qualifier_path: Path,
    harness_dir: Path,
    node_version: str,
) -> dict[str, Any]:
    harness_package_path = harness_dir / "package.json"
    harness_lock_path = harness_dir / "package-lock.json"
    if not harness_package_path.is_file() or not harness_lock_path.is_file():
        raise QualificationError("qualification harness manifest or lockfile is missing")
    harness_package = json.loads(harness_package_path.read_text(encoding="utf-8"))
    dependencies = harness_package.get("dependencies")
    if not isinstance(dependencies, dict):
        raise QualificationError("qualification harness dependencies are missing")
    required_dependencies = {
        "@modelcontextprotocol/client": "2.0.0",
        "@testcontainers/mariadb": "11.0.3",
        "@testcontainers/mssqlserver": "11.0.3",
        "@testcontainers/mysql": "11.0.3",
        "@testcontainers/postgresql": "11.0.3",
    }
    if any(
        dependencies.get(name) != version
        for name, version in required_dependencies.items()
    ):
        raise QualificationError(
            "qualification harness dependencies are not the expected exact versions"
        )
    harness_lock = json.loads(harness_lock_path.read_text(encoding="utf-8"))
    lock_packages = harness_lock.get("packages")
    if not isinstance(lock_packages, dict):
        raise QualificationError("qualification harness lock packages are missing")
    testcontainers_package = lock_packages.get("node_modules/testcontainers")
    if (
        not isinstance(testcontainers_package, dict)
        or not isinstance(testcontainers_package.get("version"), str)
    ):
        raise QualificationError(
            "qualification harness lock is missing testcontainers core"
        )
    return {
        "package": candidate["name"],
        "version": candidate["version"],
        "integrity": candidate["integrity"],
        "node_major": parse_node_version(node_version)[0],
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "mcp_client_version": dependencies["@modelcontextprotocol/client"],
        "testcontainers_adapter_version": dependencies["@testcontainers/mysql"],
        "testcontainers_core_version": testcontainers_package["version"],
        "harness_lock_sha256": sha256_file(harness_lock_path),
        "qualification_policy_version": QUALIFICATION_POLICY_VERSION,
        "matrix_sha256": sha256_file(matrix_path),
        "qualifier_sha256": sha256_file(qualifier_path),
    }


def credential_path(
    state_dir: Path,
    candidate: dict[str, str],
) -> Path:
    integrity_key = sha256_bytes(candidate["integrity"].encode("utf-8"))[:16]
    return (
        state_dir
        / "qualifications"
        / f"{candidate['version']}-{integrity_key}.json"
    )


def load_json_if_present(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def runtime_is_intact(
    credential: dict[str, Any],
    *,
    state_dir: Path | None = None,
) -> bool:
    if credential.get("schema_version") != QUALIFICATION_SCHEMA_VERSION:
        return False
    runtime = credential.get("runtime")
    if not isinstance(runtime, dict):
        return False
    root_text = runtime.get("root")
    entry_text = runtime.get("entry")
    lock_sha256 = runtime.get("package_lock_sha256")
    tree_sha256 = runtime.get("runtime_tree_sha256")
    if (
        not isinstance(root_text, str)
        or not isinstance(entry_text, str)
        or not isinstance(lock_sha256, str)
        or not isinstance(tree_sha256, str)
        or runtime.get("runtime_tree_hash_version") != 1
    ):
        return False
    root = Path(root_text)
    entry = Path(entry_text)
    if not root.is_absolute():
        return False
    if state_dir is not None:
        try:
            root.resolve().relative_to((state_dir / "runtimes").resolve())
        except (OSError, ValueError):
            return False
    lock_path = root / "package-lock.json"
    package_json = root / "node_modules/@bytebase/dbhub/package.json"
    expected_entry = root / "node_modules/@bytebase/dbhub/dist/index.js"
    if (
        entry != expected_entry
        or not lock_path.is_file()
        or not package_json.is_file()
        or not entry.is_file()
    ):
        return False
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    try:
        return (
            package.get("name") == PACKAGE_NAME
            and package.get("version") == credential.get("candidate", {}).get("version")
            and sha256_file(lock_path) == lock_sha256
            and sha256_tree(root) == tree_sha256
        )
    except (OSError, QualificationError):
        return False


def same_version_integrity_drift(
    state_dir: Path,
    candidate: dict[str, str],
) -> bool:
    qualification_dir = state_dir / "qualifications"
    if not qualification_dir.is_dir():
        return False
    for path in qualification_dir.glob(f"{candidate['version']}-*.json"):
        value = load_json_if_present(path)
        if (
            value
            and value.get("status") in REUSABLE_STATUSES
            and value.get("candidate", {}).get("integrity")
            != candidate["integrity"]
        ):
            return True
    return False


def check_release(args: argparse.Namespace) -> dict[str, Any]:
    node_version = current_node_version()
    metadata = npm_metadata(
        args.version,
        registry=args.registry,
        state_dir=args.state_dir,
    )
    candidate = validate_metadata(metadata, node_version=node_version)
    if args.version is not None and candidate["version"] != args.version:
        raise QualificationError(
            f"npm returned {candidate['version']} for requested {args.version}"
        )
    inputs = qualification_inputs(
        candidate=candidate,
        matrix_path=args.matrix,
        qualifier_path=args.qualifier,
        harness_dir=args.harness_dir,
        node_version=node_version,
    )
    fingerprint = canonical_hash(inputs)
    path = credential_path(args.state_dir, candidate)
    existing = load_json_if_present(path)

    if same_version_integrity_drift(args.state_dir, candidate):
        status = "security_blocked"
        heavy_required = False
        reason = "same version has a different previously qualified integrity"
    elif (
        existing
        and existing.get("status") in REUSABLE_STATUSES
        and existing.get("input_fingerprint") == fingerprint
        and runtime_is_intact(existing, state_dir=args.state_dir)
    ):
        status = "up_to_date"
        heavy_required = False
        reason = "exact artifact and qualification inputs already have a reusable credential"
    else:
        status = "qualification_required"
        heavy_required = True
        reason = "no matching qualification credential"

    return {
        "status": status,
        "reason": reason,
        "heavy_required": heavy_required,
        "checked_at": utc_now(),
        "node_version": node_version,
        "candidate": candidate,
        "inputs": inputs,
        "input_fingerprint": fingerprint,
        "qualification_path": str(path),
        **(
            {"qualification_status": existing.get("status")}
            if status == "up_to_date" and existing
            else {}
        ),
    }


@contextlib.contextmanager
def qualification_lock(state_dir: Path) -> Iterator[None]:
    ensure_private_directory(state_dir)
    lock_path = state_dir / "qualification.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    ):
        os.close(descriptor)
        raise QualificationError("qualification lock file is unsafe")
    os.set_inheritable(descriptor, False)
    os.fchmod(descriptor, 0o600)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.seek(0)
            owner = "".join(
                character
                for character in handle.read(256)
                if character.isprintable() or character in "\n\t"
            ).strip().replace("\n", ", ")
            detail = f" ({owner})" if owner else ""
            raise AlreadyRunningError(
                f"another qualification run is active{detail}"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nstarted_at={utc_now()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def prepare_artifact(
    *,
    candidate: dict[str, str],
    state_dir: Path,
    registry: str,
) -> Path:
    validate_registry(registry)
    qualification_dir = state_dir / "qualifications"
    if qualification_dir.is_dir():
        for path in sorted(qualification_dir.glob(f"{candidate['version']}-*.json")):
            existing = load_json_if_present(path)
            if (
                existing
                and existing.get("status") in REUSABLE_STATUSES
                and existing.get("candidate") == candidate
                and runtime_is_intact(existing, state_dir=state_dir)
            ):
                runtime = existing["runtime"]
                root = Path(runtime["root"])
                return root / "node_modules/@bytebase/dbhub"

    integrity_key = sha256_bytes(candidate["integrity"].encode("utf-8"))[:16]
    runtimes = state_dir / "runtimes"
    runtimes.mkdir(parents=True, exist_ok=True)
    os.chmod(runtimes, 0o700)
    artifact_root = Path(
        tempfile.mkdtemp(
            prefix=f"{candidate['version']}-{integrity_key}-",
            dir=runtimes,
        )
    )
    os.chmod(artifact_root, 0o700)
    package_root = artifact_root / "node_modules/@bytebase/dbhub"
    work_root, _, _ = npm_context(state_dir)
    try:
        with tempfile.TemporaryDirectory(
            prefix="pack-",
            dir=work_root,
        ) as temporary_download:
            download_dir = Path(temporary_download)
            packed = npm_json(
                [
                    "pack",
                    f"{PACKAGE_NAME}@{candidate['version']}",
                    "--json",
                    "--pack-destination",
                    str(download_dir),
                ],
                state_dir=state_dir,
                cwd=download_dir,
                timeout=600,
            )
            if not isinstance(packed, list) or len(packed) != 1:
                raise QualificationError("npm pack returned an unexpected result")
            item = packed[0]
            if item.get("integrity") != candidate["integrity"]:
                raise QualificationError(
                    "npm pack integrity does not match registry metadata"
                )
            if item.get("shasum") != candidate["shasum"]:
                raise QualificationError(
                    "npm pack shasum does not match registry metadata"
                )
            filename = item.get("filename")
            if (
                not isinstance(filename, str)
                or Path(filename).name != filename
            ):
                raise QualificationError("npm pack reported an unsafe filename")
            tarball = download_dir / filename
            if not tarball.is_file():
                raise QualificationError("npm pack tarball is missing")
            tarball_bytes = tarball.read_bytes()
            actual_integrity = "sha512-" + base64.b64encode(
                hashlib.sha512(tarball_bytes).digest()
            ).decode("ascii")
            actual_shasum = hashlib.sha1(tarball_bytes).hexdigest()
            if actual_integrity != candidate["integrity"]:
                raise QualificationError(
                    "downloaded tarball sha512 does not match metadata"
                )
            if actual_shasum != candidate["shasum"]:
                raise QualificationError(
                    "downloaded tarball sha1 does not match metadata"
                )
            runtime_tarball = artifact_root / "artifact.tgz"
            os.replace(tarball, runtime_tarball)
            os.chmod(runtime_tarball, 0o600)

        atomic_empty_file(artifact_root / ".npmrc")
        result = npm_run(
            [
                "install",
                "--prefix",
                str(artifact_root),
                "--no-audit",
                "--no-fund",
                "--ignore-scripts",
                "--save-exact",
                str(runtime_tarball),
            ],
            state_dir=state_dir,
        )
        if result.returncode != 0:
            raise QualificationError(
                f"npm install failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        validate_lock_provenance(
            artifact_root / "package-lock.json",
            local_artifact_integrity=candidate["integrity"],
        )
        installed = json.loads(
            (package_root / "package.json").read_text(encoding="utf-8")
        )
        if (
            installed.get("name") != PACKAGE_NAME
            or installed.get("version") != candidate["version"]
        ):
            raise QualificationError(
                "installed artifact identity does not match candidate"
            )
        return package_root
    except Exception:
        shutil.rmtree(artifact_root, ignore_errors=True)
        raise


def prepare_harness(*, harness_dir: Path, state_dir: Path) -> Path:
    source_package = harness_dir / "package.json"
    source_lock = harness_dir / "package-lock.json"
    validate_lock_provenance(source_lock)
    harness_key = sha256_bytes(
        source_package.read_bytes() + b"\0" + source_lock.read_bytes()
    )[:20]
    harness_runs = state_dir / "harness-runs"
    harness_runs.mkdir(parents=True, exist_ok=True)
    os.chmod(harness_runs, 0o700)
    target = Path(
        tempfile.mkdtemp(prefix=f"{harness_key}-", dir=harness_runs)
    )
    os.chmod(target, 0o700)
    try:
        shutil.copy2(source_package, target / "package.json")
        shutil.copy2(source_lock, target / "package-lock.json")
        atomic_empty_file(target / ".npmrc")
        result = npm_run(
            [
            "ci",
            "--prefix",
            str(target),
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            ],
            state_dir=state_dir,
        )
        if result.returncode != 0:
            raise QualificationError(
                f"qualification harness install failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        validate_lock_provenance(target / "package-lock.json")
        installed_client = (
            target / "node_modules/@modelcontextprotocol/client/package.json"
        )
        installed_mysql = target / "node_modules/@testcontainers/mysql/package.json"
        if (
            read_package_version(installed_client) != "2.0.0"
            or read_package_version(installed_mysql) != "11.0.3"
            or sha256_file(target / "package-lock.json") != sha256_file(source_lock)
        ):
            raise QualificationError(
                "qualification harness identity does not match its lock"
            )
        return target
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def runtime_metadata(package_root: Path, *, state_dir: Path) -> dict[str, Any]:
    artifact_root = package_root.parents[2]
    lock_path = artifact_root / "package-lock.json"
    entry_path = package_root / "dist/index.js"
    if not lock_path.is_file():
        raise QualificationError("qualified runtime is missing package-lock.json")
    if not entry_path.is_file():
        raise QualificationError("qualified runtime is missing its DBHub entry")
    dependency_tree = npm_json(
        ["ls", "--all", "--json", "--prefix", str(artifact_root)],
        state_dir=state_dir,
        timeout=600,
    )
    problems = dependency_tree.get("problems", []) if isinstance(dependency_tree, dict) else []
    if problems:
        raise QualificationError(
            "qualified runtime dependency tree has problems: "
            + "; ".join(str(problem) for problem in problems)
        )
    return {
        "root": str(artifact_root),
        "package_root": str(package_root),
        "entry": str(entry_path),
        "package_lock_sha256": sha256_file(lock_path),
        "dependency_tree_sha256": canonical_hash(dependency_tree),
        "runtime_tree_hash_version": 1,
        "runtime_tree_sha256": sha256_tree(artifact_root),
    }


def run_qualifier(
    *,
    args: argparse.Namespace,
    package_root: Path,
    harness_root: Path,
    candidate: dict[str, str],
) -> dict[str, Any]:
    output_dir = args.state_dir / "attempts"
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="qualification-result-",
        suffix=".json",
        dir=output_dir,
        delete=False,
    ) as handle:
        output_path = Path(handle.name)
    os.chmod(output_path, 0o600)
    try:
        command = [
            "node",
            str(args.qualifier),
            "--harness-root",
            str(harness_root),
            "--package-root",
            str(package_root),
            "--matrix",
            str(args.matrix),
            "--expected-version",
            candidate["version"],
            "--output",
            str(output_path),
        ]
        result = subprocess.run(
            command,
            env=safe_environment(include_docker=True),
            check=False,
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
        value: Any = None
        if output_path.stat().st_size > 0:
            value = json.loads(output_path.read_text(encoding="utf-8"))
        if result.returncode != 0 and not isinstance(value, dict):
            safe_message = (result.stderr or result.stdout).strip()
            raise QualificationError(
                f"release matrix failed: {safe_message or 'no diagnostic output'}"
            )
        if not isinstance(value, dict):
            raise QualificationError("qualifier output is not an object")
        return value
    finally:
        output_path.unlink(missing_ok=True)


def qualify_release(args: argparse.Namespace) -> dict[str, Any]:
    with qualification_lock(args.state_dir):
        check = check_release(args)
        if check["status"] == "security_blocked":
            return check
        if check["status"] == "up_to_date" and not args.force:
            return check

        candidate = check["candidate"]
        try:
            package_root = prepare_artifact(
                candidate=candidate,
                state_dir=args.state_dir,
                registry=args.registry,
            )
            harness_root = prepare_harness(
                harness_dir=args.harness_dir,
                state_dir=args.state_dir,
            )
            runtime = runtime_metadata(package_root, state_dir=args.state_dir)
            matrix_result = run_qualifier(
                args=args,
                package_root=package_root,
                harness_root=harness_root,
                candidate=candidate,
            )
            if sha256_tree(Path(runtime["root"])) != runtime["runtime_tree_sha256"]:
                raise QualificationError(
                    "qualified runtime changed while the matrix was running"
                )
            qualification_status = matrix_result.get("status")
            qualified = qualification_status in REUSABLE_STATUSES
            credential = {
                "schema_version": QUALIFICATION_SCHEMA_VERSION,
                "status": qualification_status if qualified else "failed",
                "qualified_at": utc_now(),
                "candidate": candidate,
                "input_fingerprint": check["input_fingerprint"],
                "inputs": check["inputs"],
                "runtime": runtime,
                "matrix": matrix_result,
                "project_accessed": False,
                "keychain_accessed": False,
            }
            if not qualified:
                failures = matrix_result.get("required_failures", [])
                failure_text = ", ".join(str(item) for item in failures)
                raise QualificationMatrixError(
                    "required matrix entries failed"
                    + (f": {failure_text}" if failure_text else ""),
                    matrix_result,
                )

            path = Path(check["qualification_path"])
            atomic_json_write(path, credential)
            atomic_json_write(args.state_dir / "latest-qualified.json", credential)
            return {
                "status": qualification_status,
                "heavy_required": True,
                "candidate": candidate,
                "input_fingerprint": check["input_fingerprint"],
                "qualification_path": str(path),
                "runtime": runtime,
                "matrix": matrix_result,
                "project_accessed": False,
                "keychain_accessed": False,
            }
        except Exception as error:
            attempt = {
                "schema_version": QUALIFICATION_SCHEMA_VERSION,
                "status": "failed",
                "failed_at": utc_now(),
                "candidate": candidate,
                "input_fingerprint": check["input_fingerprint"],
                "error": str(error),
                "project_accessed": False,
                "keychain_accessed": False,
            }
            if isinstance(error, QualificationMatrixError):
                attempt["matrix"] = error.result
            attempt_path = (
                args.state_dir
                / "attempts"
                / f"{candidate['version']}-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
            )
            atomic_json_write(attempt_path, attempt)
            return {
                "status": "failed",
                "heavy_required": True,
                "candidate": candidate,
                "input_fingerprint": check["input_fingerprint"],
                "attempt_path": str(attempt_path),
                "error": str(error),
                **(
                    {"matrix": error.result}
                    if isinstance(error, QualificationMatrixError)
                    else {}
                ),
                "project_accessed": False,
                "keychain_accessed": False,
            }


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--version")
    parser.add_argument("--registry", default=OFFICIAL_REGISTRY)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--qualifier", type=Path, default=DEFAULT_QUALIFIER)
    parser.add_argument("--harness-dir", type=Path, default=DEFAULT_HARNESS_DIR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check and qualify stable @bytebase/dbhub npm artifacts against a "
            "projectless local database matrix."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser(
        "check",
        help="Read-only metadata and qualification-cache check.",
    )
    add_common_arguments(check_parser)

    qualify_parser = subparsers.add_parser(
        "qualify",
        help="Qualify only when no matching credential exists.",
    )
    add_common_arguments(qualify_parser)
    qualify_parser.add_argument("--force", action="store_true")
    qualify_parser.add_argument("--timeout", type=int, default=3600)
    return parser


def normalize_paths(args: argparse.Namespace) -> None:
    for name in (
        "state_dir",
        "matrix",
        "qualifier",
        "harness_dir",
    ):
        value = getattr(args, name, None)
        if value is not None:
            setattr(args, name, value.expanduser().resolve())
    if args.version is not None and not SEMVER_PATTERN.fullmatch(args.version):
        raise QualificationError("--version must be an exact stable semver")
    args.registry = validate_registry(args.registry)
    if not args.matrix.is_file():
        raise QualificationError(f"matrix manifest is missing: {args.matrix}")
    if not args.qualifier.is_file():
        raise QualificationError(f"qualifier is missing: {args.qualifier}")
    if not args.harness_dir.is_dir():
        raise QualificationError(
            f"qualification harness directory is missing: {args.harness_dir}"
        )


def main() -> int:
    args = build_parser().parse_args()
    normalize_paths(args)
    if args.command == "check":
        result = check_release(args)
    else:
        result = qualify_release(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] not in {"failed", "blocked", "security_blocked"} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AlreadyRunningError as error:
        print(
            json.dumps(
                {
                    "status": "already_running",
                    "error": str(error),
                    "project_accessed": False,
                    "keychain_accessed": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(0)
    except (
        QualificationError,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        subprocess.TimeoutExpired,
    ) as error:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": str(error),
                    "project_accessed": False,
                    "keychain_accessed": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
