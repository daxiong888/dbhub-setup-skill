from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "manage_dbhub_release.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("manage_dbhub_release_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


manage = load_script_module()


class ManageDbhubReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "state"
        self.harness_dir = self.root / "harness"
        self.matrix = self.root / "matrix.json"
        self.qualifier = self.root / "qualify.mjs"
        self.matrix.write_text('{"matrix":[]}\n', encoding="utf-8")
        self.qualifier.write_text("// deterministic test qualifier\n", encoding="utf-8")
        self.harness_dir.mkdir()
        (self.harness_dir / "package.json").write_text(
            json.dumps(
                {
                    "private": True,
                    "dependencies": {
                        "@modelcontextprotocol/client": "2.0.0",
                        "@testcontainers/mariadb": "11.0.3",
                        "@testcontainers/mssqlserver": "11.0.3",
                        "@testcontainers/mysql": "11.0.3",
                        "@testcontainers/postgresql": "11.0.3",
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.harness_dir / "package-lock.json").write_text(
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "": {},
                        "node_modules/testcontainers": {
                            "version": "11.14.0",
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _write_package_version(path: Path, name: str, version: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"name": name, "version": version}),
            encoding="utf-8",
        )

    @staticmethod
    def metadata(
        *,
        version: str = "1.0.0",
        engine: str = ">=22.5.0",
        integrity: str = "sha512-current",
    ) -> dict[str, object]:
        return {
            "name": "@bytebase/dbhub",
            "version": version,
            "engines": {"node": engine},
            "dist": {
                "integrity": integrity,
                "shasum": "a" * 40,
                "tarball": (
                    "https://registry.npmjs.org/@bytebase/dbhub/-/dbhub-"
                    f"{version}.tgz"
                ),
            },
        }

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            version="1.0.0",
            registry=manage.OFFICIAL_REGISTRY,
            metadata_file=None,
            state_dir=self.state_dir,
            matrix=self.matrix,
            qualifier=self.qualifier,
            harness_dir=self.harness_dir,
        )

    def _runtime(self, version: str = "1.0.0") -> tuple[Path, dict[str, object]]:
        root = self.state_dir / "runtimes/qualified-runtime"
        lock = root / "package-lock.json"
        package = root / "node_modules/@bytebase/dbhub/package.json"
        entry = root / "node_modules/@bytebase/dbhub/dist/index.js"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text('{"lockfileVersion":3}\n', encoding="utf-8")
        self._write_package_version(package, "@bytebase/dbhub", version)
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// dbhub entry\n", encoding="utf-8")
        return root, {
            "root": str(root),
            "entry": str(entry),
            "package_lock_sha256": manage.sha256_file(lock),
            "runtime_tree_hash_version": 1,
            "runtime_tree_sha256": manage.sha256_tree(root),
        }

    def _qualified_credential(
        self,
        candidate: dict[str, str],
        runtime: dict[str, object],
    ) -> tuple[Path, dict[str, object]]:
        inputs = manage.qualification_inputs(
            candidate=candidate,
            matrix_path=self.matrix,
            qualifier_path=self.qualifier,
            harness_dir=self.harness_dir,
            node_version="24.11.1",
        )
        credential = {
            "schema_version": manage.QUALIFICATION_SCHEMA_VERSION,
            "status": "qualified",
            "candidate": candidate,
            "inputs": inputs,
            "input_fingerprint": manage.canonical_hash(inputs),
            "runtime": runtime,
        }
        path = manage.credential_path(self.state_dir, candidate)
        manage.atomic_json_write(path, credential)
        return path, credential

    def test_validate_metadata_requires_stable_semver_node_and_integrity(self) -> None:
        candidate = manage.validate_metadata(
            self.metadata(),
            node_version="24.11.1",
        )
        self.assertEqual(candidate["name"], "@bytebase/dbhub")
        self.assertEqual(candidate["version"], "1.0.0")
        self.assertEqual(candidate["engine"], ">=22.5.0")
        self.assertEqual(candidate["integrity"], "sha512-current")

        invalid_cases = [
            (
                self.metadata(version="1.0.0-rc.1"),
                "24.11.1",
                "exact stable semver",
            ),
            (
                self.metadata(),
                "22.4.9",
                "does not satisfy",
            ),
            (
                self.metadata(integrity="sha256-not-accepted"),
                "24.11.1",
                "sha512 dist.integrity",
            ),
            (
                {
                    **self.metadata(),
                    "dist": {
                        **self.metadata()["dist"],
                        "tarball": "https://registry.example/dbhub-1.0.0.tgz",
                    },
                },
                "24.11.1",
                "official registry",
            ),
        ]
        for metadata, node_version, expected in invalid_cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(manage.QualificationError, expected):
                    manage.validate_metadata(metadata, node_version=node_version)

    def test_check_release_reuses_only_an_exact_intact_runtime(self) -> None:
        candidate = manage.validate_metadata(
            self.metadata(),
            node_version="24.11.1",
        )
        _, runtime = self._runtime()
        self._qualified_credential(candidate, runtime)

        with (
            mock.patch.object(manage, "current_node_version", return_value="24.11.1"),
            mock.patch.object(manage, "npm_metadata", return_value=self.metadata()),
        ):
            result = manage.check_release(self.args())

        self.assertEqual(result["status"], "up_to_date")
        self.assertFalse(result["heavy_required"])
        self.assertIn("exact artifact", result["reason"])

    def test_check_release_reuses_partial_connector_credential(self) -> None:
        candidate = manage.validate_metadata(
            self.metadata(),
            node_version="24.11.1",
        )
        _, runtime = self._runtime()
        credential_path, credential = self._qualified_credential(candidate, runtime)
        credential["status"] = "partially_qualified"
        manage.atomic_json_write(credential_path, credential)

        with (
            mock.patch.object(manage, "current_node_version", return_value="24.11.1"),
            mock.patch.object(manage, "npm_metadata", return_value=self.metadata()),
        ):
            result = manage.check_release(self.args())

        self.assertEqual(result["status"], "up_to_date")
        self.assertEqual(result["qualification_status"], "partially_qualified")
        self.assertFalse(result["heavy_required"])

    def test_check_release_blocks_same_version_integrity_drift(self) -> None:
        old_candidate = manage.validate_metadata(
            self.metadata(integrity="sha512-previous"),
            node_version="24.11.1",
        )
        _, runtime = self._runtime()
        self._qualified_credential(old_candidate, runtime)

        with (
            mock.patch.object(manage, "current_node_version", return_value="24.11.1"),
            mock.patch.object(manage, "npm_metadata", return_value=self.metadata()),
        ):
            result = manage.check_release(self.args())

        self.assertEqual(result["status"], "security_blocked")
        self.assertFalse(result["heavy_required"])
        self.assertIn("different previously qualified integrity", result["reason"])

    def test_check_release_does_not_reuse_runtime_without_lock(self) -> None:
        candidate = manage.validate_metadata(
            self.metadata(),
            node_version="24.11.1",
        )
        runtime_root, runtime = self._runtime()
        self._qualified_credential(candidate, runtime)
        (runtime_root / "package-lock.json").unlink()

        with (
            mock.patch.object(manage, "current_node_version", return_value="24.11.1"),
            mock.patch.object(manage, "npm_metadata", return_value=self.metadata()),
        ):
            result = manage.check_release(self.args())

        self.assertEqual(result["status"], "qualification_required")
        self.assertTrue(result["heavy_required"])

    def test_check_release_does_not_reuse_mutated_runtime_tree(self) -> None:
        candidate = manage.validate_metadata(
            self.metadata(),
            node_version="24.11.1",
        )
        runtime_root, runtime = self._runtime()
        self._qualified_credential(candidate, runtime)
        (
            runtime_root / "node_modules/@bytebase/dbhub/dist/index.js"
        ).write_text("// mutated entry\n", encoding="utf-8")

        with (
            mock.patch.object(manage, "current_node_version", return_value="24.11.1"),
            mock.patch.object(manage, "npm_metadata", return_value=self.metadata()),
        ):
            result = manage.check_release(self.args())

        self.assertEqual(result["status"], "qualification_required")
        self.assertTrue(result["heavy_required"])

    def test_prepare_artifact_uses_unique_runtime_and_keeps_tarball(self) -> None:
        tarball_bytes = b"deterministic tarball"
        tarball_integrity = "sha512-" + base64.b64encode(
            hashlib.sha512(tarball_bytes).digest()
        ).decode("ascii")
        tarball_shasum = hashlib.sha1(tarball_bytes).hexdigest()
        metadata = self.metadata(integrity=tarball_integrity)
        metadata["dist"]["shasum"] = tarball_shasum
        candidate = manage.validate_metadata(
            metadata,
            node_version="24.11.1",
        )
        old_artifact_root = self.state_dir / "artifacts/stale-cache"
        package_root = old_artifact_root / "node_modules/@bytebase/dbhub"
        self._write_package_version(
            package_root / "package.json",
            "@bytebase/dbhub",
            candidate["version"],
        )
        self.assertFalse((old_artifact_root / "package-lock.json").exists())

        tarball_name = "bytebase-dbhub-1.0.0.tgz"

        def fake_npm_json(command, **_kwargs):
            self.assertEqual(command[0], "pack")
            destination = Path(
                command[command.index("--pack-destination") + 1]
            )
            (destination / tarball_name).write_bytes(tarball_bytes)
            return [
                {
                    "integrity": candidate["integrity"],
                    "shasum": candidate["shasum"],
                    "filename": tarball_name,
                }
            ]

        def fake_npm_run(command, **_kwargs):
            self.assertEqual(command[0], "install")
            artifact_root = Path(command[command.index("--prefix") + 1])
            installed_package_root = (
                artifact_root / "node_modules/@bytebase/dbhub"
            )
            self._write_package_version(
                installed_package_root / "package.json",
                "@bytebase/dbhub",
                candidate["version"],
            )
            (artifact_root / "package-lock.json").write_text(
                json.dumps(
                    {
                        "lockfileVersion": 3,
                        "packages": {
                            "": {},
                            "node_modules/@bytebase/dbhub": {
                                "version": candidate["version"],
                                "resolved": "file:artifact.tgz",
                                "integrity": candidate["integrity"],
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(
                manage, "npm_json", side_effect=fake_npm_json
            ) as pack,
            mock.patch.object(manage, "npm_run", side_effect=fake_npm_run) as install,
        ):
            resolved = manage.prepare_artifact(
                candidate=candidate,
                state_dir=self.state_dir,
                registry=manage.OFFICIAL_REGISTRY,
            )

        self.assertNotEqual(resolved, package_root)
        self.assertEqual(resolved.parents[2].parent, self.state_dir / "runtimes")
        pack.assert_called_once()
        install.assert_called_once()
        self.assertTrue((resolved.parents[2] / "package-lock.json").is_file())
        self.assertTrue((resolved.parents[2] / "artifact.tgz").is_file())
        self.assertTrue(old_artifact_root.is_dir())

    def test_runtime_metadata_binds_lock_and_dependency_tree(self) -> None:
        artifact_root = self.root / "artifact"
        package_root = artifact_root / "node_modules/@bytebase/dbhub"
        entry = package_root / "dist/index.js"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// entry\n", encoding="utf-8")
        lock = artifact_root / "package-lock.json"
        lock.write_text('{"lockfileVersion":3}\n', encoding="utf-8")
        dependency_tree = {
            "name": "qualified-runtime",
            "dependencies": {"@bytebase/dbhub": {"version": "1.0.0"}},
        }

        with mock.patch.object(
            manage,
            "npm_json",
            return_value=dependency_tree,
        ) as npm_ls:
            runtime = manage.runtime_metadata(
                package_root,
                state_dir=self.state_dir,
            )

        self.assertEqual(runtime["root"], str(artifact_root))
        self.assertEqual(runtime["entry"], str(entry))
        self.assertEqual(runtime["package_lock_sha256"], manage.sha256_file(lock))
        self.assertEqual(
            runtime["dependency_tree_sha256"],
            manage.canonical_hash(dependency_tree),
        )
        self.assertEqual(
            runtime["runtime_tree_sha256"],
            manage.sha256_tree(artifact_root),
        )
        npm_ls.assert_called_once()

        lock.unlink()
        with self.assertRaisesRegex(
            manage.QualificationError,
            "missing package-lock.json",
        ):
            manage.runtime_metadata(package_root, state_dir=self.state_dir)

    def test_runtime_tree_rejects_external_and_broken_symlinks(self) -> None:
        root = self.root / "tree"
        root.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (root / "escape").symlink_to(outside)
        with self.assertRaisesRegex(
            manage.QualificationError,
            "absolute symlink",
        ):
            manage.sha256_tree(root)

        (root / "escape").unlink()
        (root / "broken").symlink_to("../missing.txt")
        with self.assertRaisesRegex(
            manage.QualificationError,
            "escapes or is broken",
        ):
            manage.sha256_tree(root)

    def test_qualification_lock_uses_kernel_lock_and_keeps_lock_file(self) -> None:
        lock = self.state_dir / "qualification.lock"
        with manage.qualification_lock(self.state_dir):
            self.assertTrue(lock.is_file())
            with self.assertRaisesRegex(
                manage.AlreadyRunningError,
                "another qualification run is active",
            ):
                with manage.qualification_lock(self.state_dir):
                    self.fail("concurrent lock must not be acquired")
        self.assertTrue(lock.is_file())
        with manage.qualification_lock(self.state_dir):
            self.assertIn(f"pid={os.getpid()}", lock.read_text(encoding="utf-8"))

    def test_npm_metadata_is_isolated_to_official_registry_and_state(self) -> None:
        captured: dict[str, object] = {}

        def fake_command_json(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return self.metadata()

        hostile_environment = {
            "HOME": str(self.root / "home"),
            "PATH": os.environ.get("PATH", ""),
            "NPM_CONFIG_REGISTRY": "https://registry.example",
            "NPM_TOKEN": "must-not-propagate",
            "NODE_OPTIONS": "--require=/tmp/not-allowed.js",
        }
        with (
            mock.patch.dict(os.environ, hostile_environment, clear=True),
            mock.patch.object(
                manage,
                "command_json",
                side_effect=fake_command_json,
            ),
        ):
            metadata = manage.npm_metadata(
                "1.0.0",
                registry=manage.OFFICIAL_REGISTRY,
                state_dir=self.state_dir,
            )

        self.assertEqual(metadata["version"], "1.0.0")
        command = captured["command"]
        self.assertIn(manage.OFFICIAL_REGISTRY, command)
        self.assertIn(
            f"--@bytebase:registry={manage.OFFICIAL_REGISTRY}",
            command,
        )
        self.assertIn("--userconfig", command)
        self.assertIn("--globalconfig", command)
        self.assertIn("--cache", command)
        cwd = Path(captured["cwd"])
        cwd.resolve().relative_to((self.state_dir / "npm/work").resolve())
        environment = captured["env"]
        self.assertNotIn("NPM_CONFIG_REGISTRY", environment)
        self.assertNotIn("NPM_TOKEN", environment)
        self.assertNotIn("NODE_OPTIONS", environment)
        for name in ("empty-user.npmrc", "empty-global.npmrc"):
            path = self.state_dir / "npm" / name
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        with self.assertRaisesRegex(
            manage.QualificationError,
            "official npm registry",
        ):
            manage.validate_registry("https://registry.example")

    def test_isolated_npm_config_replaces_symlink_without_touching_target(self) -> None:
        npm_root = self.state_dir / "npm"
        npm_root.mkdir(parents=True)
        target = self.root / "must-not-change.npmrc"
        target.write_text("registry=https://registry.example\n", encoding="utf-8")
        (npm_root / "empty-user.npmrc").symlink_to(target)
        work = npm_root / "work"
        work.mkdir()
        (work / ".npmrc").write_text(
            "@bytebase:registry=https://registry.example\n",
            encoding="utf-8",
        )

        manage.npm_context(self.state_dir)

        config = npm_root / "empty-user.npmrc"
        self.assertFalse(config.is_symlink())
        self.assertEqual(config.read_text(encoding="utf-8"), "")
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "registry=https://registry.example\n",
        )
        self.assertEqual((work / ".npmrc").read_text(encoding="utf-8"), "")

    def test_lock_provenance_rejects_nonofficial_and_missing_integrity(self) -> None:
        lock = self.root / "package-lock.json"
        invalid_packages = [
            {
                "resolved": "https://registry.example/pkg/-/pkg-1.0.0.tgz",
                "integrity": "sha512-test",
            },
            {
                "resolved": "https://registry.npmjs.org:444/pkg/-/pkg-1.0.0.tgz",
                "integrity": "sha512-test",
            },
            {
                "resolved": "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz",
            },
            {},
        ]
        for package in invalid_packages:
            with self.subTest(package=package):
                lock.write_text(
                    json.dumps(
                        {
                            "packages": {
                                "": {},
                                "node_modules/pkg": package,
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(manage.QualificationError):
                    manage.validate_lock_provenance(lock)


if __name__ == "__main__":
    unittest.main()
