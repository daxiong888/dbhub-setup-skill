#!/usr/bin/env node

/**
 * Black-box qualification of an installed @bytebase/dbhub artifact.
 *
 * The script never reads a project or Keychain. Every database is a disposable
 * Testcontainers instance using synthetic credentials from the fixed matrix.
 */

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";


const SQL_MARKER = "dbhub_qualification_sql_marker";
const FIXTURE_PASSWORD = "dbhub-local-fixture-password-019fb63a";
const ROOT_PASSWORD = "dbhub-local-root-password-019fb63a";
const SQLSERVER_PASSWORD = "Dbhub_Local_Qualification_019fb63a!";
const NEVER_PULL = { shouldPull: () => false };
const SUPPORTED_JSON_SCHEMA_DIALECT =
  "https://json-schema.org/draft/2020-12/schema";
const EXPECTED_INPUT_SCHEMAS = {
  execute_sql: {
    type: "object",
    properties: {
      sql: {
        type: "string",
      },
    },
    required: ["sql"],
  },
  search_objects: {
    type: "object",
    properties: {
      object_type: {
        type: "string",
        enum: [
          "schema",
          "table",
          "view",
          "column",
          "procedure",
          "function",
          "index",
        ],
      },
      pattern: {
        type: "string",
        default: "%",
      },
      schema: {
        type: "string",
      },
      table: {
        type: "string",
      },
      detail_level: {
        type: "string",
        enum: ["names", "summary", "full"],
        default: "names",
      },
      limit: {
        type: "integer",
        default: 100,
        exclusiveMinimum: 0,
        maximum: 1000,
      },
    },
    required: ["object_type"],
  },
};


function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name?.startsWith("--") || value === undefined) {
      throw new Error(`invalid argument near ${name ?? "<end>"}`);
    }
    result[name.slice(2)] = value;
  }
  for (const required of [
    "harness-root",
    "package-root",
    "matrix",
    "expected-version",
    "output",
  ]) {
    if (!result[required]) {
      throw new Error(`missing --${required}`);
    }
  }
  return result;
}


function canonical(value) {
  if (Array.isArray(value)) {
    return value.map(canonical);
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonical(value[key])])
    );
  }
  return value;
}


function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}


function safeError(error, secrets = []) {
  let value = error instanceof Error ? error.message : String(error);
  for (const secret of secrets) {
    if (secret) {
      value = value.split(secret).join("<redacted>");
      value = value.split(encodeURIComponent(secret)).join("<redacted>");
    }
  }
  return value.replace(
    /([a-z][a-z0-9+.-]*:\/\/[^:\s/@]+:)[^@\s/]+@/gi,
    "$1<redacted>@"
  );
}


function imageIdentity(image) {
  const raw = execFileSync(
    "docker",
    [
      "image",
      "inspect",
      image,
      "--format",
      "{{.Id}}|{{.Architecture}}|{{.Os}}|{{json .RepoDigests}}",
    ],
    { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }
  ).trim();
  const [id, architecture, operatingSystem, repoDigestsJson] = raw.split("|");
  return {
    id,
    architecture,
    operatingSystem,
    repoDigests: JSON.parse(repoDigestsJson),
  };
}


function tomlString(value) {
  return JSON.stringify(value);
}


function renderConfig(connector, dsn) {
  const timezone =
    connector === "mysql" || connector === "mariadb"
      ? '\ntimezone = "+08:00"'
      : "";
  return `# Generated for an isolated DBHub release qualification run.

[[sources]]
id = "matrix"
type = ${tomlString(connector)}
dsn = ${tomlString(dsn)}
connection_timeout = 20
query_timeout = 20${timezone}
lazy = true

[[tools]]
name = "execute_sql"
source = "matrix"
readonly = true
max_rows = 3

[[tools]]
name = "search_objects"
source = "matrix"
`;
}


function renderLazyMultiSourceConfig() {
  return `# Projectless protocol and multi-source contract probe.

[[sources]]
id = "alpha"
type = "sqlite"
dsn = "sqlite:///:memory:"
lazy = true

[[sources]]
id = "beta"
type = "sqlite"
dsn = "sqlite:///:memory:"
lazy = true

[[tools]]
name = "execute_sql"
source = "alpha"
readonly = true
max_rows = 3

[[tools]]
name = "search_objects"
source = "alpha"

[[tools]]
name = "execute_sql"
source = "beta"
readonly = true
max_rows = 3

[[tools]]
name = "search_objects"
source = "beta"
`;
}


function parseToolPayload(result) {
  const text = (result.content ?? [])
    .filter((item) => item?.type === "text" || typeof item?.text === "string")
    .map((item) => item.text ?? "")
    .join("\n");
  try {
    return { text, payload: JSON.parse(text) };
  } catch {
    throw new Error("DBHub tool response was not JSON");
  }
}


export function singleStatementQueryResult(payload) {
  const data = payload?.data;
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("execute_sql response data was not an object");
  }
  if (Array.isArray(data.rows)) {
    return { rows: data.rows, count: data.count };
  }
  if (
    Array.isArray(data.statements) &&
    data.statements.length === 1 &&
    data.statements[0] !== null &&
    typeof data.statements[0] === "object" &&
    Array.isArray(data.statements[0].rows)
  ) {
    return {
      rows: data.statements[0].rows,
      count: data.statements[0].count,
    };
  }
  throw new Error("execute_sql response did not contain one result set");
}


function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}


function isolatedChildEnv(workingDirectory) {
  const env = {
    HOME: workingDirectory,
    XDG_CACHE_HOME: join(workingDirectory, ".cache"),
    XDG_CONFIG_HOME: join(workingDirectory, ".config"),
    NODE_NO_WARNINGS: "1",
  };
  for (const name of [
    "PATH",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "TZ",
    "SYSTEMROOT",
  ]) {
    if (process.env[name]) {
      env[name] = process.env[name];
    }
  }
  return env;
}


function validateManifest(manifest) {
  assert(manifest.schema_version === 1, "unsupported matrix schema");
  assert(Array.isArray(manifest.matrix) && manifest.matrix.length > 0, "empty matrix");
  const ids = new Set();
  for (const cell of manifest.matrix) {
    assert(typeof cell.id === "string" && cell.id.length > 0, "matrix cell id missing");
    assert(!ids.has(cell.id), `duplicate matrix cell id: ${cell.id}`);
    ids.add(cell.id);
    assert(
      ["mysql", "mariadb", "postgres", "sqlserver", "sqlite"].includes(
        cell.connector
      ),
      `unsupported connector in matrix: ${cell.connector}`
    );
    assert(
      typeof cell.database_version === "string" &&
        typeof cell.expected_version_pattern === "string",
      `version contract missing for ${cell.id}`
    );
    new RegExp(cell.expected_version_pattern);
    if (cell.connector !== "sqlite") {
      assert(
        typeof cell.image === "string" &&
          /@sha256:[0-9a-f]{64}$/.test(cell.image),
        `matrix image is not an exact digest for ${cell.id}`
      );
      assert(
        /^sha256:[0-9a-f]{64}$/.test(cell.image_id),
        `matrix image ID is invalid for ${cell.id}`
      );
      assert(
        /^linux\/(?:amd64|arm64)$/.test(cell.platform),
        `matrix platform is invalid for ${cell.id}`
      );
    }
  }
}


function preflightImages(manifest) {
  for (const cell of manifest.matrix) {
    if (cell.connector === "sqlite") {
      continue;
    }
    const identity = imageIdentity(cell.image);
    assert(
      identity.id === cell.image_id,
      `local image ID drift for ${cell.id}: expected ${cell.image_id}, got ${identity.id}`
    );
    assert(
      `${identity.operatingSystem}/${identity.architecture}` === cell.platform,
      `local platform drift for ${cell.id}`
    );
    assert(
      identity.repoDigests.includes(cell.image),
      `local image digest is missing for ${cell.id}`
    );
  }
}


export function localDockerSocketPath(dockerHost) {
  assert(typeof dockerHost === "string", "Docker endpoint must be a string");
  assert(
    !/[\u0000-\u001f\u007f]/u.test(dockerHost),
    "Docker endpoint contains control characters"
  );
  let endpoint;
  try {
    endpoint = new URL(dockerHost);
  } catch {
    throw new Error("qualification requires a valid local Unix Docker endpoint");
  }
  assert(
    endpoint.protocol === "unix:" &&
      endpoint.hostname === "" &&
      endpoint.username === "" &&
      endpoint.password === "" &&
      endpoint.port === "" &&
      endpoint.search === "" &&
      endpoint.hash === "",
    "qualification requires a local Unix Docker endpoint without a host or URL metadata"
  );
  assert(
    !endpoint.pathname.includes("%"),
    "qualification rejects percent-encoded Docker socket paths"
  );
  assert(
    isAbsolute(endpoint.pathname) && endpoint.pathname !== "/",
    "qualification requires an absolute Unix Docker socket path"
  );
  return endpoint.pathname;
}


function configureLocalDockerHost() {
  let dockerHost = process.env.DOCKER_HOST;
  if (!dockerHost) {
    const raw = execFileSync(
      "docker",
      [
        "context",
        "inspect",
        "--format",
        "{{json .Endpoints.docker.Host}}",
      ],
      { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }
    ).trim();
    dockerHost = JSON.parse(raw);
  }
  localDockerSocketPath(dockerHost);
  process.env.DOCKER_HOST = dockerHost;
  process.env.TESTCONTAINERS_RYUK_DISABLED = "true";
  return dockerHost;
}


function databaseVersionSql(connector) {
  if (connector === "mysql" || connector === "mariadb") {
    return "SELECT VERSION() AS dbhub_database_version";
  }
  if (connector === "postgres") {
    return "SELECT current_setting('server_version') AS dbhub_database_version";
  }
  if (connector === "sqlserver") {
    return (
      "SELECT CAST(SERVERPROPERTY('ProductVersion') AS VARCHAR(128)) " +
      "AS dbhub_database_version"
    );
  }
  if (connector === "sqlite") {
    return "SELECT sqlite_version() AS dbhub_database_version";
  }
  throw new Error(`unsupported connector: ${connector}`);
}


export function normalizedToolContract(tools) {
  const ignoredPresentationKeywords = new Set([
    "description",
    "title",
    "examples",
    "deprecated",
    "$comment",
  ]);
  const schemaMapKeywords = new Set([
    "properties",
    "patternProperties",
    "$defs",
    "definitions",
    "dependentSchemas",
  ]);
  const schemaArrayKeywords = new Set([
    "allOf",
    "anyOf",
    "oneOf",
    "prefixItems",
  ]);
  const schemaValueKeywords = new Set([
    "additionalProperties",
    "unevaluatedProperties",
    "propertyNames",
    "contains",
    "items",
    "not",
    "if",
    "then",
    "else",
    "unevaluatedItems",
    "contentSchema",
  ]);

  function schema(value, isDocumentRoot = false) {
    if (Array.isArray(value)) {
      return value.map(schema);
    }
    if (!value || typeof value !== "object") {
      return value;
    }
    const entries = Object.entries(value)
      .filter(([key, item]) => {
        if (key === "$schema" && isDocumentRoot) {
          assert(
            item === SUPPORTED_JSON_SCHEMA_DIALECT,
            `unsupported tool input schema dialect: ${String(item)}`
          );
          return false;
        }
        return !ignoredPresentationKeywords.has(key);
      })
      .sort(([left], [right]) => left.localeCompare(right));
    return Object.fromEntries(
      entries.map(([key, item]) => {
        if (
          schemaMapKeywords.has(key) &&
          item &&
          typeof item === "object" &&
          !Array.isArray(item)
        ) {
          return [
            key,
            Object.fromEntries(
              Object.entries(item)
                .sort(([left], [right]) => left.localeCompare(right))
                .map(([property, propertySchema]) => [
                  property,
                  schema(propertySchema),
                ])
            ),
          ];
        }
        if (schemaArrayKeywords.has(key) && Array.isArray(item)) {
          return [key, item.map(schema)];
        }
        if (schemaValueKeywords.has(key)) {
          return [key, schema(item)];
        }
        // Preserve every other keyword and value exactly (apart from stable
        // object-key ordering). Unknown validation semantics must fail closed.
        return [key, canonical(item)];
      })
    );
  }
  return tools
    .map((tool) => ({
      name: tool.name,
      inputSchema: schema(tool.inputSchema ?? {}, true),
    }))
    .sort((left, right) => left.name.localeCompare(right.name));
}


function expectedToolName(toolName) {
  for (const baseName of Object.keys(EXPECTED_INPUT_SCHEMAS)) {
    if (toolName === baseName || toolName.startsWith(`${baseName}_`)) {
      return baseName;
    }
  }
  return null;
}


function assertExpectedToolContract(contract, context) {
  for (const tool of contract) {
    const baseName = expectedToolName(tool.name);
    assert(baseName !== null, `${context} has unexpected tool ${tool.name}`);
    assert(
      JSON.stringify(canonical(tool.inputSchema)) ===
        JSON.stringify(canonical(EXPECTED_INPUT_SCHEMAS[baseName])),
      `${context} input schema mismatch for ${tool.name}`
    );
  }
}


export function installLocalOnlyImagePullGuard(DockerImageClient) {
  assert(
    typeof DockerImageClient?.prototype?.pull === "function" &&
      typeof DockerImageClient?.prototype?.exists === "function",
    "unsupported Testcontainers image client"
  );
  DockerImageClient.prototype.pull = async function localOnlyImagePull(imageName) {
    const exists = await this.exists(imageName);
    assert(
      exists,
      `qualification image is no longer available locally: ${imageName.string}`
    );
  };
}


async function loadHarness(harnessRoot) {
  const requireFromHarness = createRequire(join(harnessRoot, "package.json"));
  const testcontainersEntry = requireFromHarness.resolve("testcontainers");
  const dockerImageClientPath = join(
    dirname(testcontainersEntry),
    "container-runtime",
    "clients",
    "image",
    "docker-image-client.js"
  );
  const { DockerImageClient } = requireFromHarness(dockerImageClientPath);
  installLocalOnlyImagePullGuard(DockerImageClient);
  const { Client } = requireFromHarness("@modelcontextprotocol/client");
  const { StdioClientTransport } = requireFromHarness(
    "@modelcontextprotocol/client/stdio"
  );
  const { MySqlContainer } = requireFromHarness("@testcontainers/mysql");
  const { MariaDbContainer } = requireFromHarness("@testcontainers/mariadb");
  const { PostgreSqlContainer } = requireFromHarness(
    "@testcontainers/postgresql"
  );
  const { MSSQLServerContainer } = requireFromHarness(
    "@testcontainers/mssqlserver"
  );
  return {
    Client,
    StdioClientTransport,
    MySqlContainer,
    MariaDbContainer,
    PostgreSqlContainer,
    MSSQLServerContainer,
  };
}


async function startDatabase(cell, harness) {
  if (cell.connector === "sqlite") {
    return {
      dsn: "sqlite:///:memory:",
      container: null,
      image: null,
    };
  }

  const identity = imageIdentity(cell.image);
  assert(
    identity.id === cell.image_id,
    `local image ID drift for ${cell.id}: expected ${cell.image_id}, got ${identity.id}`
  );
  assert(
    `${identity.operatingSystem}/${identity.architecture}` === cell.platform,
    `local platform drift for ${cell.id}`
  );
  assert(
    identity.repoDigests.includes(cell.image),
    `local image digest is missing for ${cell.id}`
  );

  if (cell.connector === "mysql") {
    const container = await new harness.MySqlContainer(cell.image)
      .withPullPolicy(NEVER_PULL)
      .withDatabase("dbhubq")
      .withUsername("dbhubq")
      .withUserPassword(FIXTURE_PASSWORD)
      .withRootPassword(ROOT_PASSWORD)
      .start();
    return {
      dsn: container.getConnectionUri(false),
      container,
      image: identity,
    };
  }
  if (cell.connector === "mariadb") {
    const container = await new harness.MariaDbContainer(cell.image)
      .withPullPolicy(NEVER_PULL)
      .withDatabase("dbhubq")
      .withUsername("dbhubq")
      .withUserPassword(FIXTURE_PASSWORD)
      .withRootPassword(ROOT_PASSWORD)
      .start();
    return {
      dsn: container.getConnectionUri(false),
      container,
      image: identity,
    };
  }
  if (cell.connector === "postgres") {
    const container = await new harness.PostgreSqlContainer(cell.image)
      .withPullPolicy(NEVER_PULL)
      .withDatabase("dbhubq")
      .withUsername("dbhubq")
      .withPassword(FIXTURE_PASSWORD)
      .start();
    return {
      dsn: container.getConnectionUri(),
      container,
      image: identity,
    };
  }
  if (cell.connector === "sqlserver") {
    const container = await new harness.MSSQLServerContainer(cell.image)
      .withPullPolicy(NEVER_PULL)
      .acceptLicense()
      .withPassword(SQLSERVER_PASSWORD)
      .start();
    const dsn = new URL("sqlserver://127.0.0.1/master");
    dsn.hostname = container.getHost();
    dsn.port = String(container.getPort());
    dsn.username = "sa";
    dsn.password = SQLSERVER_PASSWORD;
    dsn.searchParams.set("sslmode", "disable");
    return {
      dsn: dsn.toString(),
      container,
      image: identity,
    };
  }
  throw new Error(`unsupported connector in matrix: ${cell.connector}`);
}


async function runMcpProbe({
  cell,
  dsn,
  packageRoot,
  expectedVersion,
  harness,
}) {
  const workingDirectory = mkdtempSync(join(tmpdir(), "dbhub-qualification-"));
  const configPath = join(workingDirectory, "dbhub.toml");
  writeFileSync(configPath, renderConfig(cell.connector, dsn), {
    encoding: "utf8",
    mode: 0o600,
  });
  chmodSync(configPath, 0o600);

  const entry = join(packageRoot, "dist", "index.js");
  const client = new harness.Client({
    name: "dbhub-release-qualification",
    version: "1.0.0",
  });
  const transport = new harness.StdioClientTransport({
    command: process.execPath,
    args: [
      entry,
      "--transport",
      "stdio",
      "--config",
      configPath,
    ],
    cwd: workingDirectory,
    env: isolatedChildEnv(workingDirectory),
    stderr: "pipe",
  });
  let stderr = "";
  transport.stderr?.on("data", (chunk) => {
    stderr += chunk.toString();
  });

  try {
    await client.connect(transport);
    const serverInfo = client.getServerVersion();
    assert(serverInfo?.version === expectedVersion, "serverInfo.version mismatch");

    const listed = await client.listTools();
    const contract = normalizedToolContract(listed.tools ?? []);
    const toolNames = contract.map((tool) => tool.name);
    assert(
      JSON.stringify(toolNames) ===
        JSON.stringify(["execute_sql", "search_objects"]),
      `unexpected single-source tool names: ${toolNames.join(",")}`
    );
    assertExpectedToolContract(contract, `single-source ${cell.id}`);

    const version = parseToolPayload(
      await client.callTool({
        name: "execute_sql",
        arguments: { sql: databaseVersionSql(cell.connector) },
      })
    );
    assert(version.payload.success === true, "database version query did not succeed");
    const versionResult = singleStatementQueryResult(version.payload);
    const actualDatabaseVersion = String(
      versionResult.rows[0]?.dbhub_database_version ?? ""
    );
    assert(
      new RegExp(cell.expected_version_pattern).test(actualDatabaseVersion),
      `database version mismatch: ${actualDatabaseVersion}`
    );

    const select = parseToolPayload(
      await client.callTool({
        name: "execute_sql",
        arguments: { sql: "SELECT 1 AS dbhub_connection_check" },
      })
    );
    assert(select.payload.success === true, "SELECT 1 did not succeed");
    const selectResult = singleStatementQueryResult(select.payload);
    assert(selectResult.count === 1, "SELECT 1 count was not one");
    assert(
      Number(selectResult.rows[0]?.dbhub_connection_check) === 1,
      "SELECT 1 value was not one"
    );

    const search = parseToolPayload(
      await client.callTool({
        name: "search_objects",
        arguments: {
          object_type: "schema",
          pattern: "%",
          detail_level: "names",
          limit: 2,
        },
      })
    );
    assert(search.payload.success === true, "schema search did not succeed");

    const readonlyResult = await client.callTool({
      name: "execute_sql",
      arguments: {
        sql: "UPDATE __dbhub_nonexistent_upgrade_probe__ SET value = 1",
      },
    });
    const readonlyPayload = parseToolPayload(readonlyResult).payload;
    assert(readonlyResult.isError === true, "UPDATE was not a tool error");
    assert(
      readonlyPayload.code === "READONLY_VIOLATION",
      `UPDATE returned ${readonlyPayload.code ?? "no code"}`
    );

    const recoveryOne = parseToolPayload(
      await client.callTool({
        name: "execute_sql",
        arguments: { sql: "SELECT 1 AS dbhub_connection_check" },
      })
    );
    assert(
      recoveryOne.payload.success === true,
      "connection did not recover after readonly rejection"
    );

    const missingResult = await client.callTool({
      name: "execute_sql",
      arguments: {
        sql:
          "SELECT * FROM __dbhub_nonexistent_upgrade_probe__ " +
          `/* ${SQL_MARKER} */`,
      },
    });
    const missingPayload = parseToolPayload(missingResult).payload;
    assert(missingResult.isError === true, "missing-table query was not a tool error");
    assert(
      missingPayload.code === "EXECUTION_ERROR",
      `missing-table query returned ${missingPayload.code ?? "no code"}`
    );

    const recoveryTwo = parseToolPayload(
      await client.callTool({
        name: "execute_sql",
        arguments: { sql: "SELECT 1 AS dbhub_connection_check" },
      })
    );
    assert(
      recoveryTwo.payload.success === true,
      "connection did not recover after query failure"
    );

    const rowCap = parseToolPayload(
      await client.callTool({
        name: "execute_sql",
        arguments: {
          sql:
            "SELECT 1 AS dbhub_row_cap_probe " +
            "UNION ALL SELECT 2 " +
            "UNION ALL SELECT 3 " +
            "UNION ALL SELECT 4",
        },
      })
    );
    assert(rowCap.payload.success === true, "row-cap query did not succeed");
    const rowCapResult = singleStatementQueryResult(rowCap.payload);
    const rowCapCount = rowCapResult.count;
    const rowCapLength = rowCapResult.rows.length;
    assert(
      rowCapCount === 3 && rowCapLength === 3,
      `max_rows=3 was not enforced (count=${rowCapCount}, rows=${rowCapLength})`
    );

    assert(!stderr.includes(SQL_MARKER), "stderr exposed the SQL marker");
    for (const secret of [
      FIXTURE_PASSWORD,
      ROOT_PASSWORD,
      SQLSERVER_PASSWORD,
      encodeURIComponent(FIXTURE_PASSWORD),
      encodeURIComponent(ROOT_PASSWORD),
      encodeURIComponent(SQLSERVER_PASSWORD),
    ]) {
      assert(!stderr.includes(secret), "stderr exposed a fixture credential");
    }
    assert(!/password\s*=/i.test(stderr), "stderr contained a password assignment");

    return {
      server_info: serverInfo,
      actual_database_version: actualDatabaseVersion,
      tool_names: toolNames,
      contract_sha256: sha256(JSON.stringify(canonical(contract))),
      checks: {
        initialize: true,
        tools_list: true,
        database_version: true,
        select_one: true,
        search_schema: true,
        readonly_update_rejected: true,
        recovery_after_readonly: true,
        execution_error: true,
        recovery_after_error: true,
        max_rows_three: true,
        stderr_sql_marker_absent: true,
        stderr_fixture_credentials_absent: true,
      },
    };
  } finally {
    await client.close().catch(() => {});
    rmSync(workingDirectory, { recursive: true, force: true });
  }
}


async function runCoreContract({
  packageRoot,
  expectedVersion,
  harness,
  modern,
}) {
  const workingDirectory = mkdtempSync(join(tmpdir(), "dbhub-core-contract-"));
  const configPath = join(workingDirectory, "dbhub.toml");
  writeFileSync(configPath, renderLazyMultiSourceConfig(), {
    encoding: "utf8",
    mode: 0o600,
  });
  chmodSync(configPath, 0o600);
  const clientOptions = modern
    ? {
        versionNegotiation: { mode: { pin: "2026-07-28" } },
        supportedProtocolVersions: ["2026-07-28"],
      }
    : {
        versionNegotiation: { mode: "legacy" },
        supportedProtocolVersions: ["2025-11-25"],
      };
  const client = new harness.Client(
    {
      name: modern
        ? "dbhub-release-qualification-modern"
        : "dbhub-release-qualification-legacy",
      version: "1.0.0",
    },
    clientOptions
  );
  const transport = new harness.StdioClientTransport({
    command: process.execPath,
    args: [
      join(packageRoot, "dist", "index.js"),
      "--transport",
      "stdio",
      "--config",
      configPath,
    ],
    cwd: workingDirectory,
    env: isolatedChildEnv(workingDirectory),
    stderr: "pipe",
  });
  let stderr = "";
  transport.stderr?.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  try {
    await client.connect(transport);
    const serverInfo = client.getServerVersion();
    assert(serverInfo?.version === expectedVersion, "core serverInfo.version mismatch");
    const listed = await client.listTools();
    const contract = normalizedToolContract(listed.tools ?? []);
    const toolNames = contract.map((tool) => tool.name);
    const expectedNames = [
      "execute_sql_alpha",
      "execute_sql_beta",
      "search_objects_alpha",
      "search_objects_beta",
    ];
    assert(
      JSON.stringify(toolNames) === JSON.stringify(expectedNames),
      `unexpected multi-source tool names: ${toolNames.join(",")}`
    );
    assertExpectedToolContract(
      contract,
      modern ? "modern multi-source" : "legacy multi-source"
    );
    assert(!stderr.includes("Skipping"), "one or more database drivers were skipped");
    return {
      mode: modern ? "modern" : "legacy",
      status: "pass",
      negotiated_protocol: client.getNegotiatedProtocolVersion(),
      server_info: serverInfo,
      tool_names: toolNames,
      contract_sha256: sha256(JSON.stringify(canonical(contract))),
    };
  } finally {
    await client.close().catch(() => {});
    rmSync(workingDirectory, { recursive: true, force: true });
  }
}


async function runCell(cell, context) {
  const startedAt = Date.now();
  let database;
  const secrets = [FIXTURE_PASSWORD, ROOT_PASSWORD, SQLSERVER_PASSWORD];
  try {
    database = await startDatabase(cell, context.harness);
    const probe = await runMcpProbe({
      cell,
      dsn: database.dsn,
      packageRoot: context.packageRoot,
      expectedVersion: context.expectedVersion,
      harness: context.harness,
    });
    return {
      id: cell.id,
      connector: cell.connector,
      database_version: cell.database_version,
      required: cell.required !== false,
      status: "pass",
      duration_ms: Date.now() - startedAt,
      image: cell.image ?? null,
      image_id: database.image?.id ?? null,
      platform: cell.platform ?? `${process.platform}/${process.arch}`,
      emulated:
        Boolean(cell.platform) &&
        !cell.platform.endsWith(`/${process.arch === "arm64" ? "arm64" : process.arch}`),
      ...probe,
    };
  } catch (error) {
    return {
      id: cell.id,
      connector: cell.connector,
      database_version: cell.database_version,
      required: cell.required !== false,
      status: "fail",
      duration_ms: Date.now() - startedAt,
      image: cell.image ?? null,
      platform: cell.platform ?? `${process.platform}/${process.arch}`,
      error: safeError(error, secrets),
    };
  } finally {
    await database?.container?.stop().catch(() => {});
  }
}


async function main() {
  const args = parseArgs(process.argv.slice(2));
  const harnessRoot = resolve(args["harness-root"]);
  const packageRoot = resolve(args["package-root"]);
  const matrixPath = resolve(args.matrix);
  const outputPath = resolve(args.output);
  const packageJson = JSON.parse(
    readFileSync(join(packageRoot, "package.json"), "utf8")
  );
  assert(packageJson.name === "@bytebase/dbhub", "artifact package name mismatch");
  assert(
    packageJson.version === args["expected-version"],
    "artifact package version mismatch"
  );

  const manifest = JSON.parse(readFileSync(matrixPath, "utf8"));
  validateManifest(manifest);
  configureLocalDockerHost();
  preflightImages(manifest);
  const harness = await loadHarness(harnessRoot);
  const legacyCore = await runCoreContract({
    packageRoot,
    expectedVersion: args["expected-version"],
    harness,
    modern: false,
  });
  const modernCore = await runCoreContract({
    packageRoot,
    expectedVersion: args["expected-version"],
    harness,
    modern: true,
  });
  assert(
    legacyCore.contract_sha256 === modernCore.contract_sha256,
    "legacy and modern protocol tool contracts differ"
  );
  const core = {
    status: "pass",
    legacy: legacyCore,
    modern: modernCore,
    contract_sha256: legacyCore.contract_sha256,
  };
  const results = [];
  for (const cell of manifest.matrix) {
    results.push(
      await runCell(cell, {
        harness,
        packageRoot,
        expectedVersion: args["expected-version"],
      })
    );
  }
  const requiredFailures = results.filter(
    (item) => item.required && item.status !== "pass"
  );
  const connectorSummary = {};
  for (const connector of [
    ...new Set(manifest.matrix.map((cell) => cell.connector)),
  ]) {
    const requiredCells = results.filter(
      (item) => item.connector === connector && item.required
    );
    const failedCells = requiredCells.filter((item) => item.status !== "pass");
    connectorSummary[connector] = {
      status: failedCells.length === 0 ? "pass" : "fail",
      required_cells: requiredCells.map((item) => item.id),
      failed_cells: failedCells.map((item) => item.id),
    };
  }
  const passedConnectorCount = Object.values(connectorSummary).filter(
    (item) => item.status === "pass"
  ).length;
  const qualificationStatus =
    requiredFailures.length === 0
      ? "qualified"
      : passedConnectorCount > 0
        ? "partially_qualified"
        : "failed";
  const output = {
    schema_version: 1,
    status: qualificationStatus,
    package: {
      name: packageJson.name,
      version: packageJson.version,
    },
    core,
    matrix_manifest_sha256: sha256(readFileSync(matrixPath)),
    matrix: results,
    connector_summary: connectorSummary,
    required_failures: requiredFailures.map((item) => item.id),
    known_limitations: manifest.known_limitations ?? [],
    project_accessed: false,
    keychain_accessed: false,
  };
  writeFileSync(outputPath, `${JSON.stringify(output, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  chmodSync(outputPath, 0o600);
  return qualificationStatus === "failed" ? 1 : 0;
}


const invokedDirectly =
  process.argv[1] !== undefined &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href;

if (invokedDirectly) {
  try {
    process.exitCode = await main();
  } catch (error) {
    process.stderr.write(`${safeError(error)}\n`);
    process.exitCode = 1;
  }
}
