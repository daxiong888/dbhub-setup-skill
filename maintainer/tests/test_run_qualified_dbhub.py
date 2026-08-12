from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "run_qualified_dbhub.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("run_qualified_dbhub_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = load_script_module()


class RunQualifiedDbhubTest(unittest.TestCase):
    VERSION = "1.0.0"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "state"
        self.runtime_root = self.state_dir / "runtimes/runtime"
        self.lock = self.runtime_root / "package-lock.json"
        self.package_json = (
            self.runtime_root / "node_modules/@bytebase/dbhub/package.json"
        )
        self.entry = self.runtime_root / "node_modules/@bytebase/dbhub/dist/index.js"
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text('{"lockfileVersion":3}\n', encoding="utf-8")
        self.package_json.parent.mkdir(parents=True, exist_ok=True)
        self.package_json.write_text(
            json.dumps(
                {
                    "name": "@bytebase/dbhub",
                    "version": self.VERSION,
                }
            ),
            encoding="utf-8",
        )
        self.entry.parent.mkdir(parents=True, exist_ok=True)
        self.entry.write_text("// immutable entry\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _credential(
        self,
        *,
        suffix: str = "test",
        status: str = "qualified",
        node_major: int = 24,
        candidate_name: str = "@bytebase/dbhub",
        candidate_version: str = VERSION,
    ) -> Path:
        inputs = runner.current_contract_inputs()
        inputs.update(
            {
                "package": candidate_name,
                "version": candidate_version,
                "integrity": "sha512-test",
            }
        )
        inputs["node_major"] = node_major
        path = (
            self.state_dir
            / "qualifications"
            / f"{self.VERSION}-{suffix}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": runner.QUALIFICATION_SCHEMA_VERSION,
                    "status": status,
                    "candidate": {
                        "name": candidate_name,
                        "version": candidate_version,
                        "integrity": "sha512-test",
                    },
                    "inputs": inputs,
                    "input_fingerprint": runner.canonical_hash(inputs),
                    "runtime": {
                        "root": str(self.runtime_root),
                        "entry": str(self.entry),
                        "package_lock_sha256": runner.sha256_file(self.lock),
                        "runtime_tree_hash_version": 1,
                        "runtime_tree_sha256": runner.sha256_tree(
                            self.runtime_root
                        ),
                    },
                    "matrix": {
                        "connector_summary": {
                            "mysql": {
                                "status": "pass",
                                "required_cells": ["mysql-test"],
                                "failed_cells": [],
                            },
                            "sqlserver": {
                                "status": "fail",
                                "required_cells": ["sqlserver-test"],
                                "failed_cells": ["sqlserver-test"],
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def _write_config(
        self,
        *,
        source_id: str = "analytics",
        connector: str = "mysql",
        lazy: bool = True,
        readonly: bool = True,
        max_rows: int = 100,
        password: str | None = None,
        include_search: bool = True,
        extra_source: str = "",
        extra_tool: str = "",
    ) -> Path:
        config = self.root / "dbhub.toml"
        password_value = password or runner.expected_password_placeholder(source_id)
        timezone = '\ntimezone = "+08:00"' if connector in {"mysql", "mariadb"} else ""
        search = (
            f'\n[[tools]]\nname = "search_objects"\nsource = "{source_id}"\n'
            if include_search
            else ""
        )
        config.write_text(
            f"""[[sources]]
id = "{source_id}"
description = "Generated test source"
type = "{connector}"
host = "127.0.0.1"
port = 3306
database = "app"
user = "readonly"
password = "{password_value}"
connection_timeout = 20
query_timeout = 20{timezone}
lazy = {str(lazy).lower()}
{extra_source}

[[tools]]
name = "execute_sql"
source = "{source_id}"
readonly = {str(readonly).lower()}
max_rows = {max_rows}
{extra_tool}
{search}""",
            encoding="utf-8",
        )
        os.chmod(config, 0o600)
        return config

    def test_load_credential_requires_exactly_one_qualified_match(self) -> None:
        with self.assertRaisesRegex(
            runner.RunnerError,
            "expected one qualified runtime.*found 0",
        ):
            runner.load_credential(self.state_dir, self.VERSION)

        expected = self._credential(suffix="primary")
        self._credential(suffix="failed", status="failed")
        resolved, _ = runner.load_credential(self.state_dir, self.VERSION)
        self.assertEqual(resolved, expected)

        self._credential(suffix="duplicate")
        with self.assertRaisesRegex(
            runner.RunnerError,
            "expected one qualified runtime.*found 2",
        ):
            runner.load_credential(self.state_dir, self.VERSION)

    def test_resolve_entry_accepts_complete_matching_runtime(self) -> None:
        credential_path = self._credential()

        with mock.patch.object(runner, "node_major", return_value=24):
            resolved_credential, resolved_entry = runner.resolve_entry(
                self.state_dir,
                self.VERSION,
            )

        self.assertEqual(resolved_credential, credential_path)
        self.assertEqual(resolved_entry, self.entry)

    def test_resolve_entry_rejects_node_major_drift(self) -> None:
        self._credential(node_major=22)

        with (
            mock.patch.object(runner, "node_major", return_value=24),
            self.assertRaisesRegex(
                runner.RunnerError,
                "qualification credential is stale for: node_major",
            ),
        ):
            runner.resolve_entry(self.state_dir, self.VERSION)

    def test_resolve_entry_rejects_old_policy_and_input_tampering(self) -> None:
        credential_path = self._credential()
        credential = json.loads(credential_path.read_text(encoding="utf-8"))
        credential["inputs"]["qualification_policy_version"] = 1
        credential["input_fingerprint"] = runner.canonical_hash(credential["inputs"])
        credential_path.write_text(json.dumps(credential), encoding="utf-8")
        with self.assertRaisesRegex(
            runner.RunnerError,
            "qualification_policy_version",
        ):
            runner.resolve_entry(self.state_dir, self.VERSION)

        credential["inputs"]["qualification_policy_version"] = (
            runner.QUALIFICATION_POLICY_VERSION
        )
        credential["input_fingerprint"] = "0" * 64
        credential_path.write_text(json.dumps(credential), encoding="utf-8")
        with self.assertRaisesRegex(runner.RunnerError, "fingerprint mismatch"):
            runner.resolve_entry(self.state_dir, self.VERSION)

    def test_resolve_entry_rejects_lock_drift_and_missing_runtime_files(self) -> None:
        self._credential()
        self.lock.write_text('{"lockfileVersion":3,"changed":true}\n', encoding="utf-8")

        with (
            mock.patch.object(runner, "node_major", return_value=24),
            self.assertRaisesRegex(runner.RunnerError, "package-lock.json has changed"),
        ):
            runner.resolve_entry(self.state_dir, self.VERSION)

        self.lock.unlink()
        with (
            mock.patch.object(runner, "node_major", return_value=24),
            self.assertRaisesRegex(runner.RunnerError, "runtime files are missing"),
        ):
            runner.resolve_entry(self.state_dir, self.VERSION)

    def test_resolve_entry_rejects_runtime_content_mutation(self) -> None:
        self._credential()
        self.entry.write_text("// mutated entry\n", encoding="utf-8")

        with (
            mock.patch.object(runner, "node_major", return_value=24),
            self.assertRaisesRegex(
                runner.RunnerError,
                "content tree has changed",
            ),
        ):
            runner.resolve_entry(self.state_dir, self.VERSION)

    def test_resolve_entry_rejects_runtime_outside_state(self) -> None:
        credential_path = self._credential()
        credential = json.loads(credential_path.read_text(encoding="utf-8"))
        outside = self.root / "outside-runtime"
        self.runtime_root.rename(outside)
        credential["runtime"]["root"] = str(outside)
        credential["runtime"]["entry"] = str(
            outside / "node_modules/@bytebase/dbhub/dist/index.js"
        )
        credential["runtime"]["runtime_tree_sha256"] = runner.sha256_tree(outside)
        credential_path.write_text(json.dumps(credential), encoding="utf-8")

        with (
            mock.patch.object(runner, "node_major", return_value=24),
            self.assertRaisesRegex(
                runner.RunnerError,
                "outside the qualification state",
            ),
        ):
            runner.resolve_entry(self.state_dir, self.VERSION)

    def test_resolve_entry_rejects_candidate_and_installed_package_identity(self) -> None:
        self._credential(candidate_name="@bytebase/not-dbhub")
        with (
            mock.patch.object(runner, "node_major", return_value=24),
            self.assertRaisesRegex(runner.RunnerError, "package name mismatch"),
        ):
            runner.resolve_entry(self.state_dir, self.VERSION)

        for path in (self.state_dir / "qualifications").glob("*.json"):
            path.unlink()
        self._credential()
        self.package_json.write_text(
            json.dumps(
                {
                    "name": "@bytebase/dbhub",
                    "version": "1.0.1",
                }
            ),
            encoding="utf-8",
        )
        with (
            mock.patch.object(runner, "node_major", return_value=24),
            self.assertRaisesRegex(runner.RunnerError, "package identity mismatch"),
        ):
            runner.resolve_entry(self.state_dir, self.VERSION)

    def test_partial_credential_and_config_connector_gate(self) -> None:
        credential_path = self._credential(status="partially_qualified")
        credential = json.loads(credential_path.read_text(encoding="utf-8"))
        config = self._write_config()
        runner.validate_config_coverage(
            credential,
            [f"--config={config}"],
        )

        config = self._write_config(
            source_id="warehouse",
            connector="sqlserver",
        )
        with self.assertRaisesRegex(
            runner.RunnerError,
            "does not cover project connectors: sqlserver",
        ):
            runner.validate_config_coverage(
                credential,
                ["--config", str(config)],
            )

    def test_config_contract_rejects_unsafe_mutations(self) -> None:
        credential_path = self._credential()
        credential = json.loads(credential_path.read_text(encoding="utf-8"))
        cases = [
            (
                {"lazy": False},
                "lazy = true",
            ),
            (
                {"password": "plaintext-secret"},
                "environment placeholder",
            ),
            (
                {"readonly": False},
                "readonly = true",
            ),
            (
                {"max_rows": 0},
                "invalid max_rows",
            ),
            (
                {"include_search": False},
                "exactly execute_sql and search_objects",
            ),
            (
                {"extra_source": 'init_script = "unsafe.sql"'},
                "generated source contract",
            ),
            (
                {"extra_tool": 'custom_option = true'},
                "generated contract",
            ),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                config = self._write_config(**arguments)
                with self.assertRaisesRegex(runner.RunnerError, expected):
                    runner.validate_config_file(credential, config)

        config = self._write_config()
        os.chmod(config, 0o644)
        with self.assertRaisesRegex(runner.RunnerError, "mode must be 600"):
            runner.validate_config_file(credential, config)

    def test_config_contract_binds_hash_and_rejects_symlink(self) -> None:
        credential_path = self._credential()
        credential = json.loads(credential_path.read_text(encoding="utf-8"))
        config = self._write_config()
        expected_hash = runner.sha256_file(config)
        runner.validate_config_file(
            credential,
            config,
            expected_sha256=expected_hash,
        )
        config.write_text(
            config.read_text(encoding="utf-8") + "\n# changed\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(runner.RunnerError, "content hash has changed"):
            runner.validate_config_file(
                credential,
                config,
                expected_sha256=expected_hash,
            )

        target = self._write_config()
        symlink = self.root / "linked-dbhub.toml"
        symlink.symlink_to(target)
        with self.assertRaisesRegex(runner.RunnerError, "regular file"):
            runner.validate_config_file(credential, symlink)

    def test_validate_config_cli_checks_runtime_and_contract_without_exec(self) -> None:
        self._credential(status="partially_qualified")
        config = self._write_config()
        argv = [
            "run_qualified_dbhub.py",
            "--version",
            self.VERSION,
            "--state-dir",
            str(self.state_dir),
            "--expected-config-sha256",
            runner.sha256_file(config),
            "--validate-config",
            str(config),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(runner, "node_major", return_value=24),
            mock.patch.object(runner.os, "execvpe") as execvpe,
        ):
            self.assertEqual(runner.main(), 0)
        execvpe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
