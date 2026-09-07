#!/usr/bin/env node

"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");
const Database = require("better-sqlite3");

const cases = [
  {
    name: "most-issues",
    query:
      "SELECT r.full_name, COUNT(*) AS count FROM issues i JOIN repos r ON r.id=i.repo_id GROUP BY r.id ORDER BY count DESC LIMIT 1",
    expected: [{ full_name: "microsoft/winget-pkgs", count: 60 }],
  },
  {
    name: "merged-prs",
    query: "SELECT SUM(merged) AS merged, COUNT(*) AS total FROM pulls",
    expected: [{ merged: 6894, total: 16483 }],
  },
  {
    name: "vercel-repos",
    query: "SELECT name FROM repos WHERE owner='vercel' ORDER BY name",
    expected: [
      "ai",
      "ai-chatbot",
      "commerce",
      "hyper",
      "next.js",
      "nft",
      "storage",
      "swr",
      "turbo",
      "vercel",
    ].map((name) => ({ name })),
  },
  {
    name: "most-repositories",
    query:
      "SELECT owner, COUNT(*) AS count FROM repos GROUP BY owner ORDER BY count DESC LIMIT 1",
    expected: [{ owner: "favstats", count: 170 }],
  },
];

function branchEnvironment() {
  for (const candidate of [
    "/etc/smolvm/branch-env",
    "/etc/smolvm/fork-env",
  ]) {
    if (!fs.existsSync(candidate)) continue;
    const values = {};
    for (const line of fs.readFileSync(candidate, "utf8").split("\n")) {
      const separator = line.indexOf("=");
      if (separator > 0) {
        values[line.slice(0, separator)] = line.slice(separator + 1);
      }
    }
    return values;
  }
  return {};
}

function writeJsonAtomic(destination, payload) {
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const temporary = `${destination}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(payload)}\n`);
  fs.renameSync(temporary, destination);
}

function waitForFile(destination) {
  while (!fs.existsSync(destination)) {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
  }
}

const resultDir = process.env.RESULT_DIR || null;
const initialProvider = process.env.BENCH_PROVIDER || "smol";
const initialRun = process.env.BENCH_RUN_ID || "unassigned";
const initialIndex = Number.parseInt(process.env.BENCH_INDEX || "0", 10);
const initializedAt = process.hrtime.bigint();
const database = new Database("data/database.sqlite", { readonly: true });
database.pragma("cache_size = -524288");
const statements = cases.map((testCase) => database.prepare(testCase.query));

// Warm the exact ordinary queries before the branch boundary. The statements,
// SQLite page cache and open file descriptor are all part of the captured state.
for (let pass = 0; pass < 2; pass += 1) {
  for (const statement of statements) statement.all();
}
const warmAt = process.hrtime.bigint();

writeJsonAtomic(
  resultDir
    ? path.join(
        resultDir,
        `ready-${initialProvider}-${initialRun}-${initialIndex}.json`,
      )
    : "/tmp/braintrust-worker-ready.json",
  {
    pid: process.pid,
    initialize_ms: Number(warmAt - initializedAt) / 1e6,
    provider: initialProvider,
    run_id: initialRun,
    index: initialIndex,
  },
);

if (process.env.SMOL_BRANCH === "1") {
  const helper = spawnSync("smolvm-branch-ready", [], { stdio: "inherit" });
  if (helper.status !== 0) {
    throw new Error(`smolvm-branch-ready exited ${helper.status}`);
  }
} else if (process.env.DOCKER_GATE === "1") {
  if (!resultDir) throw new Error("DOCKER_GATE requires RESULT_DIR");
  waitForFile(path.join(resultDir, `go-${initialProvider}-${initialRun}`));
}

const assigned = { ...process.env, ...branchEnvironment() };
const provider = assigned.BENCH_PROVIDER || initialProvider;
const runId = assigned.BENCH_RUN_ID || initialRun;
const index = Number.parseInt(
  assigned.BENCH_INDEX || assigned.SMOLVM_BRANCH_INDEX || String(initialIndex),
  10,
);
const testCase = cases[index % cases.length];
const started = process.hrtime.bigint();
const observed = statements[index % statements.length].all();
const completed = process.hrtime.bigint();
const correct = JSON.stringify(observed) === JSON.stringify(testCase.expected);
const resultPath = resultDir
  ? path.join(resultDir, `result-${provider}-${runId}-${index}.json`)
  : "/tmp/braintrust-result.json";
const payload = {
  pid: process.pid,
  provider,
  run_id: runId,
  index,
  case: testCase.name,
  query_ms: Number(completed - started) / 1e6,
  correct,
  observed,
  expected: testCase.expected,
};

// Publish the task result before the diagnostic repeat. The harness measures
// only the first execution; repeating the identical statement shows whether a
// restored worker paid one-time page activation or remains intrinsically slow.
writeJsonAtomic(resultPath, payload);
const repeatStarted = process.hrtime.bigint();
const repeatObserved = statements[index % statements.length].all();
const repeatCompleted = process.hrtime.bigint();
payload.repeat_query_ms = Number(repeatCompleted - repeatStarted) / 1e6;
payload.repeat_correct =
  JSON.stringify(repeatObserved) === JSON.stringify(testCase.expected);
writeJsonAtomic(resultPath, payload);

// Keep the completed environment resident until the harness samples memory and
// removes it. This wait is outside the task-completion latency.
setInterval(() => {}, 60_000);
