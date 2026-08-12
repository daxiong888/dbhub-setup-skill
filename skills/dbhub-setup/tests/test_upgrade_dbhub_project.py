from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "upgrade_dbhub_project.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("upgrade_dbhub_project_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


upgrade = load_script_module()


class UpgradeDbhubProjectTest(unittest.TestCase):
    TARGET = "1.0.0"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.project_root = self.root / "project"
        self.state_dir = self.root / "state"
        self.dbhub_dir = self.project_root / ".codex/dbhub"
        self.dbhub_dir.mkdir(parents=True)
        self.launcher = self.dbhub_dir / "start-dbhub.zsh"
        self.config = self.dbhub_dir / "dbhub.toml"
        self._write_launcher()
        self._write_config(
            [
                ("analytics_blue", "mysql"),
                ("warehouse_green", "mysql"),
            ]
        )
        self._write_qualification({"mysql"})

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_launcher(self) -> None:
        self.launcher.write_text(
            upgrade.render_legacy_npx_launcher(
                version="0.24.0",
                keychain_service="codex.dbhub.test",
                loads=[
                    (
                        "analytics_blue",
                        ["DBHUB_ANALYTICS_BLUE_PASSWORD"],
                    ),
                    (
                        "warehouse_green",
                        ["DBHUB_WAREHOUSE_GREEN_PASSWORD"],
                    ),
                ],
            ),
            encoding="utf-8",
        )
        os.chmod(self.launcher, 0o700)

    def _write_config(self, sources: list[tuple[str, str]]) -> None:
        blocks = []
        for source_id, connector in sources:
            timezone = (
                '\ntimezone = "+08:00"'
                if connector in {"mysql", "mariadb"}
                else ""
            )
            password_env = upgrade.load_qualified_runner().expected_password_placeholder(
                source_id
            )
            blocks.append(
                f"""[[sources]]
id = "{source_id}"
description = "Generated test source"
type = "{connector}"
host = "127.0.0.1"
port = 3306
database = "app"
user = "readonly"
password = "{password_env}"
connection_timeout = 20
query_timeout = 20{timezone}
lazy = true

[[tools]]
name = "execute_sql"
source = "{source_id}"
readonly = true
max_rows = 100

[[tools]]
name = "search_objects"
source = "{source_id}"
"""
            )
        self.config.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
        os.chmod(self.config, 0o600)

    def _write_qualification(self, connectors: set[str]) -> Path:
        runner = upgrade.load_qualified_runner()
        inputs = runner.current_contract_inputs()
        inputs.update(
            {
                "package": "@bytebase/dbhub",
                "version": self.TARGET,
                "integrity": "sha512-test",
            }
        )
        runtime_root = self.state_dir / "runtimes/runtime"
        lock = runtime_root / "package-lock.json"
        package = runtime_root / "node_modules/@bytebase/dbhub/package.json"
        entry = runtime_root / "node_modules/@bytebase/dbhub/dist/index.js"
        entry.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text('{"lockfileVersion":3}\n', encoding="utf-8")
        package.write_text(
            json.dumps(
                {"name": "@bytebase/dbhub", "version": self.TARGET}
            ),
            encoding="utf-8",
        )
        entry.write_text("// qualified runtime\n", encoding="utf-8")
        path = self.state_dir / "qualifications/1.0.0-test.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": runner.QUALIFICATION_SCHEMA_VERSION,
                    "status": "qualified",
                    "candidate": {
                        "name": "@bytebase/dbhub",
                        "version": self.TARGET,
                        "integrity": "sha512-test",
                    },
                    "inputs": inputs,
                    "input_fingerprint": runner.canonical_hash(inputs),
                    "runtime": {
                        "root": str(runtime_root),
                        "entry": str(entry),
                        "package_lock_sha256": runner.sha256_file(lock),
                        "runtime_tree_hash_version": 1,
                        "runtime_tree_sha256": runner.sha256_tree(runtime_root),
                    },
                    "matrix": {
                        "connector_summary": {
                            connector: {
                                "status": "pass",
                                "required_cells": [f"{connector}-test"],
                                "failed_cells": [],
                            }
                            for connector in sorted(connectors)
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_plan_is_a_dry_run_and_uses_actual_source_ids(self) -> None:
        original = self.launcher.read_text(encoding="utf-8")

        result = upgrade.project_plan(
            project_root=self.project_root,
            target=self.TARGET,
            state_dir=self.state_dir,
            apply=False,
        )

        self.assertEqual(result["mode"], "plan")
        self.assertEqual(result["current_version"], "0.24.0")
        self.assertEqual(result["target_version"], self.TARGET)
        self.assertTrue(result["changed"])
        self.assertEqual(result["runtime_transition"], "npx-to-qualified")
        self.assertEqual(
            result["sources"],
            [
                {"id": "analytics_blue", "type": "mysql"},
                {"id": "warehouse_green", "type": "mysql"},
            ],
        )
        self.assertFalse(result["real_database_tested"])
        self.assertEqual(self.launcher.read_text(encoding="utf-8"), original)
        serialized = json.dumps(result).lower()
        self.assertNotIn('"uat"', serialized)
        self.assertNotIn('"prod"', serialized)

    def test_apply_replaces_npx_with_qualified_runner_and_preserves_mode(self) -> None:
        preview = upgrade.project_plan(
            project_root=self.project_root,
            target=self.TARGET,
            state_dir=self.state_dir,
            apply=False,
        )
        with self.assertRaisesRegex(
            upgrade.UpgradeError,
            "explicit confirmation",
        ):
            upgrade.project_plan(
                project_root=self.project_root,
                target=self.TARGET,
                state_dir=self.state_dir,
                apply=True,
            )
        result = upgrade.project_plan(
            project_root=self.project_root,
            target=self.TARGET,
            state_dir=self.state_dir,
            apply=True,
            confirmed_keychain_mapping=preview["keychain_mapping_sha256"],
        )

        updated = self.launcher.read_text(encoding="utf-8")
        self.assertEqual(result["mode"], "apply")
        self.assertEqual(result["runtime_transition"], "npx-to-qualified")
        self.assertTrue(result["keychain_mapping_confirmation_required"])
        self.assertNotIn("exec npx", updated)
        self.assertIn("run_qualified_dbhub.py", updated)
        self.assertIn('exec "${PYTHON_EXECUTABLE}" "${QUALIFIED_RUNNER}"', updated)
        self.assertIn("--expected-config-sha256", updated)
        self.assertIn("--validate-config", updated)
        self.assertNotIn("DBHUB_CONFIG_PATH", updated)
        self.assertNotIn("DBHUB_KEYCHAIN_SERVICE", updated)
        self.assertNotIn("DBHUB_QUALIFIED_RUNNER", updated)
        self.assertNotIn("DBHUB_QUALIFICATION_STATE_DIR", updated)
        self.assertLess(
            updated.index("--validate-config"),
            updated.rindex("\nload_all_passwords\n"),
        )
        self.assertIn("--version 1.0.0 \\", updated)
        self.assertIn('--config="${CONFIG_PATH}"', updated)
        self.assertEqual(stat.S_IMODE(self.launcher.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

        second = upgrade.project_plan(
            project_root=self.project_root,
            target=self.TARGET,
            state_dir=self.state_dir,
            apply=False,
        )
        self.assertFalse(second["changed"])
        self.assertEqual(second["runtime_transition"], "qualified")

    def test_connector_gate_is_derived_from_toml_not_environment_names(self) -> None:
        self._write_config(
            [
                ("customer_primary", "mysql"),
                ("audit_replica", "postgres"),
            ]
        )

        with self.assertRaisesRegex(
            upgrade.UpgradeError,
            "does not cover project connectors: postgres",
        ):
            upgrade.project_plan(
                project_root=self.project_root,
                target=self.TARGET,
                state_dir=self.state_dir,
                apply=False,
            )

    def test_rejects_legacy_launcher_with_unrecognized_code(self) -> None:
        original = self.launcher.read_text(encoding="utf-8")
        self.launcher.write_text(
            original.replace("set -eu\n", "set -eu\nprint -- injected\n", 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            upgrade.UpgradeError,
            "not an exact generated template",
        ):
            upgrade.project_plan(
                project_root=self.project_root,
                target=self.TARGET,
                state_dir=self.state_dir,
                apply=False,
            )

    def test_optional_matrix_entries_do_not_satisfy_connector_gate(self) -> None:
        qualification_path = self.state_dir / "qualifications/1.0.0-test.json"
        qualification = json.loads(
            qualification_path.read_text(encoding="utf-8")
        )
        qualification["matrix"]["connector_summary"] = {
            "mysql": {
                "status": "fail",
                "required_cells": [],
                "failed_cells": [],
            }
        }
        qualification_path.write_text(
            json.dumps(qualification),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            upgrade.UpgradeError,
            "does not cover project connectors: mysql",
        ):
            upgrade.project_plan(
                project_root=self.project_root,
                target=self.TARGET,
                state_dir=self.state_dir,
                apply=False,
            )


if __name__ == "__main__":
    unittest.main()
