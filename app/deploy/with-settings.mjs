#!/usr/bin/env node
// Runs a command with the dashboard deploy settings in its environment. Every mise task that needs settings goes through
// this file, on a dev box, in an agent sandbox and in CI alike. OP_VAULT decides the source (environment, app/.env or
// app/.env.local; a repository variable in CI):
//   unset: plain environment variables, filled in from app/.env or app/.env.local (git-ignored) for names that are not
//          already set. No 1Password needed.
//   set:   `op run --env-file 1password.env -- <command>`: the committed reference file names fields in the item
//          ai-job-search in that vault. `op` signs in with the 1Password desktop app on a workstation, or
//          OP_SERVICE_ACCOUNT_TOKEN (a service account) in a sandbox or CI. Values never touch disk; op masks them.
// Both at once is an error, never a silent choice: with OP_VAULT set, the Cloudflare names must not also be set.
//   node deploy/with-settings.mjs --show                     # which source and which names are set
//   node deploy/with-settings.mjs <command> [args]  # run a command with the settings
import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const NAMES = ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_HOSTNAME", "CLOUDFLARE_ACCESS_ALLOWED_EMAILS"];
const OPTIONAL = ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCESS_TEAM_NAME", "TOFU_ALLOW_DESTROY", "TF_ENCRYPTION"];
export const ENV_FILE = "1password.env";

/** KEY=VALUE lines; # comments, blank lines, `export `, and single or double quotes are handled. Nothing is executed. */
export function parseDotenv(text) {
  const out = {};
  for (const raw of text.split(/\r?\n/)) {
    const m = raw.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!m || raw.trimStart().startsWith("#")) continue;
    let v = m[2];
    if (/^(["']).*\1$/.test(v)) v = v.slice(1, -1);
    out[m[1]] = v;
  }
  return out;
}

/** Decide the source and build the child environment. Pure, so it can be tested. */
export function resolveSettings(env, dotenv, { hasOp }) {
  const merged = { ...dotenv, ...env }; // a real environment variable wins over .env / .env.local
  const child = { ...merged };
  if (merged.OP_VAULT) {
    // Mixed sources are refused: these would be silently overridden here, or silently used if OP_VAULT went missing.
    const both = [...NAMES, "TF_ENCRYPTION"].filter((n) => merged[n]);
    if (both.length) return { child, error: `OP_VAULT is set (1Password), but ${both.join(", ")} ${both.length > 1 ? "are" : "is"} also set (GitHub secrets, app/.env or the environment). Delete ${both.length > 1 ? "them" : "it"}, or unset OP_VAULT to use plain variables. \`mise run setup\` switches cleanly.` };
    if (!hasOp) return { error: "OP_VAULT is set but the 1Password CLI (`op`) is missing. Run `mise install` (it installs the pinned, checksummed op)." };
    const files = [ENV_FILE];
    return { source: `1Password vault ${merged.OP_VAULT} (op run --env-file ${files.join(" --env-file ")})`, child, viaOp: true, files, set: NAMES.map((n) => [n, true]) };
  }
  const fromFile = NAMES.some((n) => dotenv[n] && !env[n]);
  const label = fromFile ? "environment + app/.env or app/.env.local (OP_VAULT not set)" : "environment variables (OP_VAULT not set)";
  return { source: label, child, viaOp: false, set: NAMES.map((n) => [n, Boolean(merged[n])]) };
}

function main(argv) {
  const dir = join(dirname(fileURLToPath(import.meta.url)), "..");
  const dotenv = {};
  for (const name of [".env", ".env.local"]) { const f = join(dir, name); if (existsSync(f)) Object.assign(dotenv, parseDotenv(readFileSync(f, "utf8"))); }
  const hasOp = spawnSync("op", ["--version"], { stdio: "ignore" }).status === 0;
  const r = resolveSettings(process.env, dotenv, { hasOp });
  // The names preflight still runs on a configuration error, so it can report it (and CI posts that report).
  if (r.error && !(argv.includes("--preflight-names") && r.child)) { console.error(`error: ${r.error}`); return 1; }
  if (r.error) r.source = "invalid", r.viaOp = false, r.set = NAMES.map((n) => [n, Boolean(r.child[n])]), r.child.PF_CONFIG_ERROR = r.error;
  if (argv[0] === "--show") {
    console.log(`Settings source: ${r.source}`);
    for (const [n, ok] of r.set) console.log(`  ${ok ? "set    " : "MISSING"} ${n}${r.viaOp ? " (resolved by op run at run time)" : ""}`);
    console.log(`  optional: ${OPTIONAL.filter((n) => dotenv[n] || process.env[n]).join(", ") || "none set"}`);
    return 0;
  }
  if (!argv.length) { console.error("usage: with-settings.mjs --show | <command> [args]"); return 2; }
  console.error(`settings: ${r.source}`);
  const [cmd, args] = r.viaOp ? ["op", ["run", ...r.files.flatMap((f) => ["--env-file", join(dir, f)]), "--", ...argv]] : [argv[0], argv.slice(1)];
  const res = spawnSync(cmd, args, { env: r.child, stdio: "inherit" }); // the caller's directory (mise sets it per task)
  if (res.error) { console.error(`error: could not run ${cmd}: ${res.error.code || res.error.message}`); return 1; }
  return res.status ?? 1;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) process.exit(main(process.argv.slice(2)));
