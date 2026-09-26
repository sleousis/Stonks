// npm audit for production dependencies with an allowlist (Phase 12.8).
//
// Runs `npm audit --omit=dev --json` in the current directory (web/), drops
// advisories listed in .github/audit/npm-audit-allowlist.json, and fails on
// anything left at or above the threshold (default: moderate).
//
// Usage (from web/): node ../.github/audit/npm-audit.mjs [--level=high]

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const LEVELS = ["info", "low", "moderate", "high", "critical"];
const levelArg = process.argv.find((a) => a.startsWith("--level="));
const threshold = LEVELS.indexOf(levelArg ? levelArg.split("=")[1] : "moderate");
if (threshold < 0) {
  console.error(`unknown level; use one of ${LEVELS.join(", ")}`);
  process.exit(2);
}

const here = dirname(fileURLToPath(import.meta.url));
const allowlist = JSON.parse(readFileSync(join(here, "npm-audit-allowlist.json"), "utf8"));
const allowed = new Set((allowlist.allow ?? []).map((entry) => entry.id));

let raw;
try {
  raw = execFileSync("npm", ["audit", "--omit=dev", "--json"], {
    encoding: "utf8",
    shell: process.platform === "win32",
  });
} catch (err) {
  // npm audit exits 1 when it finds anything; the JSON is still on stdout.
  raw = err.stdout;
  if (!raw) throw err;
}
const report = JSON.parse(raw);

// Advisory ids from the "via" objects (GHSA-xxxx is the tail of the URL).
const findings = [];
for (const [name, vuln] of Object.entries(report.vulnerabilities ?? {})) {
  for (const via of vuln.via ?? []) {
    if (typeof via !== "object") continue; // transitive pointer to another package
    const id = (via.url ?? "").split("/").pop() || String(via.source);
    findings.push({ name, id, severity: via.severity, title: via.title, url: via.url });
  }
}

const blocking = findings.filter(
  (f) => LEVELS.indexOf(f.severity) >= threshold && !allowed.has(f.id),
);
const ignored = findings.filter((f) => allowed.has(f.id));

for (const f of ignored) console.log(`allowed   ${f.severity.padEnd(8)} ${f.name} ${f.id}`);
for (const f of blocking) console.log(`BLOCKING  ${f.severity.padEnd(8)} ${f.name} ${f.id} ${f.title} ${f.url}`);

const stale = [...allowed].filter((id) => !findings.some((f) => f.id === id));
for (const id of stale) console.log(`note: allowlist entry ${id} no longer matches a finding; remove it`);

if (blocking.length > 0) {
  console.log(`\n${blocking.length} finding(s) at or above "${LEVELS[threshold]}".`);
  console.log("Fix them, or add an entry with a reason to .github/audit/npm-audit-allowlist.json.");
  process.exit(1);
}
console.log(`npm audit: no blocking findings (${findings.length} total, ${ignored.length} allowed).`);
