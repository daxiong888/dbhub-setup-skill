from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
QUALIFIER = SKILL_ROOT / "scripts/qualify_dbhub_release.mjs"
MATRIX = SKILL_ROOT / "scripts/dbhub_release_matrix.json"


class QualifierSafetyContractTest(unittest.TestCase):
    def test_pull_policy_is_synchronous_and_ryuk_is_disabled(self) -> None:
        source = QUALIFIER.read_text(encoding="utf-8")
        self.assertIn(
            "const NEVER_PULL = { shouldPull: () => false };",
            source,
        )
        self.assertNotIn("shouldPull: async", source)
        self.assertIn(
            'process.env.TESTCONTAINERS_RYUK_DISABLED = "true";',
            source,
        )
        self.assertIn("installLocalOnlyImagePullGuard(DockerImageClient)", source)

    def test_docker_endpoint_must_be_a_local_unix_socket(self) -> None:
        source = QUALIFIER.read_text(encoding="utf-8")
        self.assertIn('endpoint.protocol === "unix:"', source)
        self.assertIn('endpoint.hostname === ""', source)
        self.assertIn("isAbsolute(endpoint.pathname)", source)
        self.assertNotIn('dockerHost.startsWith("unix://")', source)
        self.assertNotIn('dockerHost.startsWith("tcp://")', source)
        self.assertNotIn('dockerHost.startsWith("ssh://")', source)

    def test_tool_input_contract_is_compared_to_an_exact_baseline(self) -> None:
        source = QUALIFIER.read_text(encoding="utf-8")
        self.assertIn("const EXPECTED_INPUT_SCHEMAS", source)
        self.assertIn('default: "%"', source)
        self.assertIn('default: "names"', source)
        self.assertIn("exclusiveMinimum: 0", source)
        self.assertIn("maximum: 1000", source)
        self.assertIn("assertExpectedToolContract(contract", source)
        self.assertIn("Unknown validation semantics must fail closed", source)

    def test_runtime_safety_helpers_fail_closed(self) -> None:
        script = f"""
import assert from "node:assert/strict";
import {{
  installLocalOnlyImagePullGuard,
  localDockerSocketPath,
  normalizedToolContract,
  singleStatementQueryResult,
}} from {json.dumps(QUALIFIER.as_uri())};

assert.equal(localDockerSocketPath("unix:///tmp/docker.sock"), "/tmp/docker.sock");
for (const endpoint of [
  "unix://remote.example/var/run/docker.sock",
  "unix://user@/tmp/docker.sock",
  "unix:///tmp/docker.sock?remote=1",
  "unix:///tmp/docker.sock#remote",
  "unix://relative",
  "tcp://127.0.0.1:2375",
  "ssh://host/run/docker.sock",
]) {{
  assert.throws(() => localDockerSocketPath(endpoint), /qualification|Docker endpoint/);
}}

let lowerLevelPullCalls = 0;
class FakeDockerImageClient {{
  constructor(exists) {{
    this.present = exists;
    this.dockerode = {{
      pull: async () => {{
        lowerLevelPullCalls += 1;
      }},
    }};
  }}
  async exists() {{
    return this.present;
  }}
  async pull() {{
    await this.dockerode.pull();
  }}
}}
installLocalOnlyImagePullGuard(FakeDockerImageClient);
await new FakeDockerImageClient(true).pull({{ string: "present@sha256:abc" }});
await assert.rejects(
  new FakeDockerImageClient(false).pull({{ string: "missing@sha256:def" }}),
  /no longer available locally/
);
assert.equal(lowerLevelPullCalls, 0);

for (const payload of [
  {{ data: {{ rows: [{{ dbhub_database_version: "legacy" }}], count: 1 }} }},
  {{
    data: {{
      statements: [{{ rows: [{{ dbhub_database_version: "modern" }}], count: 1 }}],
    }},
  }},
]) {{
  const result = singleStatementQueryResult(payload);
  assert.equal(result.rows.length, 1);
  assert.equal(result.count, 1);
}}
assert.throws(
  () => singleStatementQueryResult({{ data: {{ statements: [] }} }}),
  /did not contain one result set/
);

const baselineSchema = {{
  type: "object",
  properties: {{ sql: {{ type: "string" }} }},
  required: ["sql"],
}};
const baseline = normalizedToolContract([
  {{ name: "execute_sql", inputSchema: baselineSchema }},
]);
for (const [keyword, value] of [
  ["pattern", "^SELECT"],
  ["format", "uri"],
  ["const", "SELECT 1"],
  ["$ref", "#/$defs/sql"],
  ["oneOf", [{{ type: "string" }}]],
  ["minItems", 1],
  ["minProperties", 1],
  ["dependentRequired", {{ sql: ["other"] }}],
]) {{
  const mutated = structuredClone(baselineSchema);
  mutated.properties.sql[keyword] = value;
  assert.notDeepEqual(
    normalizedToolContract([{{ name: "execute_sql", inputSchema: mutated }}]),
    baseline,
    `semantic keyword ${{keyword}} was discarded`
  );
}}
const displayOnly = structuredClone(baselineSchema);
displayOnly.description = "display text";
displayOnly.properties.sql.title = "SQL";
displayOnly.$schema = "https://json-schema.org/draft/2020-12/schema";
assert.deepEqual(
  normalizedToolContract([{{ name: "execute_sql", inputSchema: displayOnly }}]),
  baseline
);
const unsupportedDialect = structuredClone(baselineSchema);
unsupportedDialect.$schema = "https://example.invalid/unknown-dialect";
assert.throws(
  () =>
    normalizedToolContract([
      {{ name: "execute_sql", inputSchema: unsupportedDialect }},
    ]),
  /unsupported tool input schema dialect/
);
const nestedDialect = structuredClone(baselineSchema);
nestedDialect.properties.sql.$schema =
  "https://json-schema.org/draft/2020-12/schema";
assert.notDeepEqual(
  normalizedToolContract([
    {{ name: "execute_sql", inputSchema: nestedDialect }},
  ]),
  baseline,
  "nested $schema was incorrectly discarded"
);
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_container_image_is_pinned_by_digest_id_and_platform(self) -> None:
        manifest = json.loads(MATRIX.read_text(encoding="utf-8"))
        for cell in manifest["matrix"]:
            if cell["connector"] == "sqlite":
                continue
            with self.subTest(cell=cell["id"]):
                self.assertRegex(cell["image"], r"@sha256:[0-9a-f]{64}$")
                self.assertRegex(cell["image_id"], r"^sha256:[0-9a-f]{64}$")
                self.assertRegex(cell["platform"], r"^linux/(?:amd64|arm64)$")
                self.assertIsNone(re.search(r":latest(?:@|$)", cell["image"]))


if __name__ == "__main__":
    unittest.main()
