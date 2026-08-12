from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = SKILL_ROOT / "scripts" / "setup_dbhub_project.py"


class SetupDbhubProjectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)
        subprocess.run(
            ["git", "init", "-q"],
            cwd=self.project_root,
            check=True,
            capture_output=True,
            text=True,
        )
        codex_dir = self.project_root / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            '[features]\nplugins = true\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def command(self, *extra: str) -> list[str]:
        return [
            sys.executable,
            str(SETUP_SCRIPT),
            "--project-root",
            str(self.project_root),
            "--source",
            "prod|mysql|prod.example|3306|app_db|readonly_user|PROD",
            "--source",
            "uat|mysql|uat.example|3306|app_db|root|UAT",
            "--keychain-service",
            "codex.dbhub.test-project",
            *extra,
        ]

    def test_generates_multi_environment_setup_without_secrets(self) -> None:
        result = subprocess.run(
            self.command(),
            check=True,
            capture_output=True,
            text=True,
        )
        summary = json.loads(result.stdout)
        self.assertEqual(summary["mode"], "write")
        self.assertEqual(
            summary["keychain"]["service"], "codex.dbhub.test-project"
        )
        self.assertEqual(
            [item["account"] for item in summary["keychain"]["credentials"]],
            ["prod", "uat"],
        )
        for credential in summary["keychain"]["credentials"]:
            self.assertTrue(credential["create_or_update"].endswith(" -w"))

        codex_config = (self.project_root / ".codex/config.toml").read_text(
            encoding="utf-8"
        )
        dbhub_config = (self.project_root / ".codex/dbhub/dbhub.toml").read_text(
            encoding="utf-8"
        )
        launcher_path = self.project_root / ".codex/dbhub/start-dbhub.zsh"
        launcher = launcher_path.read_text(encoding="utf-8")

        self.assertIn("[features]", codex_config)
        self.assertIn("[mcp_servers.dbhub]", codex_config)
        self.assertIn("args = []", codex_config)
        self.assertIn('id = "prod"', dbhub_config)
        self.assertIn('id = "uat"', dbhub_config)
        self.assertIn('password = "${DBHUB_PROD_PASSWORD}"', dbhub_config)
        self.assertIn('password = "${DBHUB_UAT_PASSWORD}"', dbhub_config)
        self.assertEqual(dbhub_config.count('timezone = "+08:00"'), 2)
        self.assertEqual(dbhub_config.count("lazy = true"), 2)
        self.assertEqual(dbhub_config.count("readonly = true"), 2)
        self.assertNotIn("actual-password", dbhub_config)
        self.assertIn("DBHUB_PROD_PASSWORD", launcher)
        self.assertIn("DBHUB_UAT_PASSWORD", launcher)
        self.assertIn("codex.dbhub.test-project", launcher)
        self.assertIn("find-generic-password", launcher)
        self.assertIn("add-generic-password", launcher)
        self.assertNotIn("display dialog", launcher)
        self.assertNotIn("prompt_for_password", launcher)
        self.assertIn("@bytebase/dbhub@1.2.0", launcher)
        self.assertIn("--registry=https://registry.npmjs.org", launcher)
        self.assertIn("--ignore-scripts", launcher)
        self.assertIn("EXPECTED_CONFIG_SHA256", launcher)
        self.assertIn(
            hashlib.sha256(dbhub_config.encode("utf-8")).hexdigest(),
            launcher,
        )
        self.assertNotIn("run_qualified_dbhub.py", launcher)
        self.assertNotIn("QUALIFICATION", launcher)
        self.assertNotIn("DBHUB_CONFIG_PATH", launcher)
        self.assertNotIn("DBHUB_KEYCHAIN_SERVICE", launcher)
        self.assertLess(
            launcher.index("config hash does not match"),
            launcher.rindex("\nload_all_passwords\n"),
        )
        self.assertEqual(summary["dbhub_version"], "1.2.0")
        self.assertTrue(summary["dbhub_version_verified"])
        self.assertEqual(summary["runtime_mode"], "npx-exact")

        self.assertEqual(
            stat.S_IMODE((self.project_root / ".codex/config.toml").stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(
                (self.project_root / ".codex/dbhub/dbhub.toml").stat().st_mode
            ),
            0o600,
        )
        self.assertEqual(stat.S_IMODE(launcher_path.stat().st_mode), 0o700)

        exclude_path = self.project_root / ".git/info/exclude"
        self.assertIn(".codex/", exclude_path.read_text(encoding="utf-8"))

        subprocess.run(["zsh", "-n", str(launcher_path)], check=True)
        check_env = os.environ.copy()
        check_env.update(
            {
                "DBHUB_PROD_PASSWORD": "dummy-prod",
                "DBHUB_UAT_PASSWORD": "dummy-uat",
                "DBHUB_LAUNCHER_CHECK_ONLY": "1",
            }
        )
        launcher_result = subprocess.run(
            [str(launcher_path)],
            env=check_env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("launcher check passed", launcher_result.stdout)
        self.assertNotIn("dummy-prod", launcher_result.stdout)
        self.assertNotIn("dummy-uat", launcher_result.stdout)

    def test_dry_run_does_not_write(self) -> None:
        result = subprocess.run(
            self.command("--dry-run"),
            check=True,
            capture_output=True,
            text=True,
        )
        summary = json.loads(result.stdout)
        self.assertEqual(summary["mode"], "dry-run")
        self.assertFalse((self.project_root / ".codex/dbhub").exists())
        self.assertNotIn(
            "[mcp_servers.dbhub]",
            (self.project_root / ".codex/config.toml").read_text(encoding="utf-8"),
        )

    def test_refuses_to_replace_existing_setup_without_force(self) -> None:
        subprocess.run(self.command(), check=True, capture_output=True, text=True)
        preview = subprocess.run(
            self.command("--dry-run"),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertTrue(json.loads(preview.stdout)["replacement_required"])

        second = subprocess.run(
            self.command(),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("--force", second.stderr)

        forced = subprocess.run(
            self.command("--force"),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(forced.stdout)["mode"], "write")
        codex_config = (self.project_root / ".codex/config.toml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(codex_config.count("[mcp_servers.dbhub]"), 1)
        self.assertIn("[features]", codex_config)

    def test_force_replaces_nested_dbhub_table_without_leaving_old_env(self) -> None:
        config_path = self.project_root / ".codex/config.toml"
        config_path.write_text(
            "\n".join(
                [
                    "[features]",
                    "plugins = true",
                    "",
                    "[mcp_servers.dbhub]",
                    'command = "old-command"',
                    "",
                    "[mcp_servers.dbhub.env]",
                    'OLD_PASSWORD = "must-not-remain"',
                    "",
                    "[mcp_servers.other]",
                    'command = "other-command"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            self.command("--force"),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout)["mode"], "write")
        updated = config_path.read_text(encoding="utf-8")
        self.assertNotIn("OLD_PASSWORD", updated)
        self.assertNotIn("old-command", updated)
        self.assertIn("[mcp_servers.other]", updated)
        self.assertIn('command = "other-command"', updated)

    def test_shell_quotes_labels(self) -> None:
        command = [
            sys.executable,
            str(SETUP_SCRIPT),
            "--project-root",
            str(self.project_root),
            "--source",
            "prod|mysql|prod.example|3306|app_db|readonly_user|PROD $SAFE",
            "--keychain-service",
            "codex.dbhub.test-project",
            "--dbhub-version",
            "1.0.0",
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        launcher_path = self.project_root / ".codex/dbhub/start-dbhub.zsh"
        subprocess.run(["zsh", "-n", str(launcher_path)], check=True)
        launcher = launcher_path.read_text(encoding="utf-8")
        dbhub_config = (self.project_root / ".codex/dbhub/dbhub.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn("PROD $SAFE", dbhub_config)
        self.assertIn("@bytebase/dbhub@1.0.0", launcher)

    def test_rejects_launcher_registry_override(self) -> None:
        result = subprocess.run(
            self.command(
                "--npm-registry",
                "https://registry.example",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("official npm registry", result.stderr)

    def test_rejects_non_exact_dbhub_version(self) -> None:
        result = subprocess.run(
            self.command("--dbhub-version", "latest"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact stable semver", result.stderr)

    def test_marks_an_explicit_nonverified_version(self) -> None:
        result = subprocess.run(
            self.command("--dbhub-version", "1.1.0", "--dry-run"),
            check=True,
            capture_output=True,
            text=True,
        )
        summary = json.loads(result.stdout)
        self.assertEqual(summary["dbhub_version"], "1.1.0")
        self.assertFalse(summary["dbhub_version_verified"])

    def test_uses_requested_mysql_timezone(self) -> None:
        subprocess.run(
            self.command("--mysql-timezone", "+09:00"),
            check=True,
            capture_output=True,
            text=True,
        )
        dbhub_config = (self.project_root / ".codex/dbhub/dbhub.toml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(dbhub_config.count('timezone = "+09:00"'), 2)

    def test_rejects_invalid_mysql_timezone(self) -> None:
        result = subprocess.run(
            self.command("--mysql-timezone", "Asia/Shanghai"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--mysql-timezone must be", result.stderr)

    def test_does_not_write_mysql_timezone_for_postgres(self) -> None:
        command = [
            sys.executable,
            str(SETUP_SCRIPT),
            "--project-root",
            str(self.project_root),
            "--source",
            "prod|postgres|prod.example|5432|app_db|readonly_user|PROD",
            "--dbhub-version",
            "1.0.0",
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        dbhub_config = (self.project_root / ".codex/dbhub/dbhub.toml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("timezone =", dbhub_config)

    def test_groups_multiple_databases_under_one_keychain_account(self) -> None:
        command = [
            sys.executable,
            str(SETUP_SCRIPT),
            "--project-root",
            str(self.project_root),
            "--source",
            (
                "prod_app|mysql|prod.example|3306|app|readonly_user|"
                "PROD app|prod"
            ),
            "--source",
            (
                "prod_identity|mysql|prod.example|3306|identity|readonly_user|"
                "PROD identity|prod"
            ),
            "--keychain-service",
            "codex.dbhub.test-project",
            "--dbhub-version",
            "1.0.0",
        ]
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        summary = json.loads(result.stdout)
        self.assertEqual(len(summary["keychain"]["credentials"]), 1)
        self.assertEqual(
            summary["keychain"]["credentials"][0]["sources"],
            ["prod_app", "prod_identity"],
        )

        launcher = (
            self.project_root / ".codex/dbhub/start-dbhub.zsh"
        ).read_text(encoding="utf-8")
        self.assertEqual(launcher.count("load_keychain_password \\"), 1)
        self.assertIn("DBHUB_PROD_APP_PASSWORD", launcher)
        self.assertIn("DBHUB_PROD_IDENTITY_PASSWORD", launcher)

        check_env = os.environ.copy()
        check_env.update(
            {
                "DBHUB_PROD_APP_PASSWORD": "dummy",
                "DBHUB_PROD_IDENTITY_PASSWORD": "dummy",
                "DBHUB_LAUNCHER_CHECK_ONLY": "1",
            }
        )
        subprocess.run(
            [str(self.project_root / ".codex/dbhub/start-dbhub.zsh")],
            env=check_env,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_rejects_shared_keychain_account_for_different_connections(self) -> None:
        command = [
            sys.executable,
            str(SETUP_SCRIPT),
            "--project-root",
            str(self.project_root),
            "--source",
            "prod_a|mysql|prod-a.example|3306|db_a|user_a|PROD A|prod",
            "--source",
            "prod_b|mysql|prod-b.example|3306|db_b|user_b|PROD B|prod",
            "--dbhub-version",
            "1.0.0",
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different type, host, port, or user", result.stderr)


class PublicSkillBoundaryTest(unittest.TestCase):
    def test_does_not_bundle_maintainer_release_tooling(self) -> None:
        excluded_paths = [
            SKILL_ROOT / ".DS_Store",
            SKILL_ROOT / "qualification",
            SKILL_ROOT / "scripts" / "dbhub_release_matrix.json",
            SKILL_ROOT / "scripts" / "manage_dbhub_release.py",
            SKILL_ROOT / "scripts" / "qualify_dbhub_release.mjs",
            SKILL_ROOT / "scripts" / "run_qualified_dbhub.py",
            SKILL_ROOT / "scripts" / "upgrade_dbhub_project.py",
        ]
        self.assertEqual(
            [str(path.relative_to(SKILL_ROOT)) for path in excluded_paths if path.exists()],
            [],
        )


if __name__ == "__main__":
    unittest.main()
