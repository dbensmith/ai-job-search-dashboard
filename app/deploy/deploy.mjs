#!/usr/bin/env node
// Plan-first, idempotent deploy for the dashboard (a static site served as Worker assets behind Cloudflare Access).
//   OpenTofu  : infra/bootstrap: the R2 bucket that holds OpenTofu state (applied only when the bucket is missing)
//   OpenTofu  : infra: Zero Trust organization (only if the account has none), the Access policy and application, the Worker
//               (a 404 stub plus the static files of site/ as assets) and its custom domain
//   site/     : built beforehand by `python tools/build_report.py --out site`; this script only uploads what is there
//
//   node deploy/deploy.mjs --preflight-names   # checks every required secret/variable is set; says which are missing and where
//   node deploy/deploy.mjs --preflight-token   # checks the token works and has each permission; names the missing ones
//   node deploy/deploy.mjs --dry-run       # read-only PLAN (pull requests): creates nothing, not even the state bucket
//   node deploy/deploy.mjs                 # PLAN: ensures the state bucket, then tofu init, validate, plan
//   node deploy/deploy.mjs --apply         # applies exactly the saved plan, smoke-tests
//
// Environment (never from the repo):
//   CLOUDFLARE_API_TOKEN (secret), CLOUDFLARE_HOSTNAME (a hostname on a zone already in the account),
//   CLOUDFLARE_ACCESS_ALLOWED_EMAILS (comma-separated), TF_ENCRYPTION (state and plan encryption; setup generates it)
// Optional: CLOUDFLARE_ACCOUNT_ID (default: the account that owns the hostname's zone), CLOUDFLARE_ACCESS_TEAM_NAME (default:
// the zone's first label; used only when the account has no Zero Trust team yet)
// The R2 S3 credentials OpenTofu needs for its state are derived from the same API token, so no second secret exists.
import { createHash } from "node:crypto";
import { appendFileSync, existsSync, readFileSync, rmSync, statSync, readdirSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const APP = join(dirname(fileURLToPath(import.meta.url)), "..");
const INFRA = join(APP, "infra");
const BOOTSTRAP = join(INFRA, "bootstrap");
const BOOT_OVERRIDE = join(BOOTSTRAP, "backend_override.tf"); // only during the first bootstrap apply
const BOOT_LOCAL_STATE = join(BOOTSTRAP, "bootstrap.tfstate"); // moved into the bucket right after
const PLAN = join(INFRA, "dashboard.tfplan");
const SITE = join(APP, "..", "site"); // built by tools/build_report.py
export const STATE_BUCKET = "ai-job-search-tfstate";
const ORG_ADDR = "cloudflare_zero_trust_organization.org";

export class DeployError extends Error {}

/** Where the deploy settings come from: OP_VAULT set means 1Password (`op run --env-file 1password.env`, see
 * deploy/with-settings.mjs), unset means plain environment variables (GitHub secrets in CI). */
export function secretsMode(env) {
  return env.OP_VAULT ? "onepassword" : "env";
}
// One entry per setting the deploy needs, and how GitHub stores it in env mode.
const SETTINGS = [
  { name: "CLOUDFLARE_API_TOKEN", kind: "secret" },
  { name: "CLOUDFLARE_HOSTNAME", kind: "secret" },
  { name: "CLOUDFLARE_ACCESS_ALLOWED_EMAILS", kind: "secret" },
];
const VALUE_RULES = {
  CLOUDFLARE_HOSTNAME: [(v) => /^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$/.test(v.toLowerCase()), "Must be a hostname such as dashboard.example.com (no https://, no path)."],
  CLOUDFLARE_ACCESS_ALLOWED_EMAILS: [(v) => { const e = v.split(",").map((s) => s.trim()).filter(Boolean); return e.length > 0 && e.every((x) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(x)); }, "Must be comma-separated email addresses."],
};
const HELP = {
  CLOUDFLARE_API_TOKEN: "Create the token (app/DEPLOY.md step 1) and save it.",
  CLOUDFLARE_HOSTNAME: "A hostname on a zone in your Cloudflare account, for example dashboard.example.com.",
  CLOUDFLARE_ACCESS_ALLOWED_EMAILS: "The email address(es) allowed to sign in, comma-separated.",
};

/**
 * Every problem with the deploy settings, as {name, kind, fix} (kind: secret | variable | environment), so CI can list them all at once.
 * env mode: the values come from the environment (GitHub secrets in CI). 1password mode: the values were injected by `op run`
 * from app/1password.env (vault OP_VAULT), so a missing one is fixed in the 1Password item.
 */
export function envProblems(env) {
  const problems = [];
  const miss = (name, kind, fix) => problems.push({ name, kind, fix });
  const op = secretsMode(env) === "onepassword";
  for (const { name, kind } of SETTINGS) {
    const value = env[name];
    const k = op ? "environment" : kind;
    const pre = op ? `Set this field in the 1Password item ai-job-search (vault ${env.OP_VAULT}); \`mise run setup --source 1password\` creates it. ` : "";
    if (!value) miss(name, k, `${pre}${HELP[name]}`);
    else if (VALUE_RULES[name] && !VALUE_RULES[name][0](value)) miss(name, k, `${pre}${VALUE_RULES[name][1]}`);
  }
  const teamName = (env.CLOUDFLARE_ACCESS_TEAM_NAME || "").toLowerCase();
  if (teamName && !/^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/.test(teamName)) miss("CLOUDFLARE_ACCESS_TEAM_NAME", "variable", "Optional; must be a short lowercase name (letters, digits, hyphens), or delete it.");
  return problems;
}

export function checkEnv(env) {
  const problems = envProblems({ ...env, OP_VAULT: "" });
  if (problems.length) {
    const absent = problems.filter((p) => !env[p.name]).map((p) => p.name);
    throw new DeployError(absent.length ? `Missing environment variables: ${absent.join(", ")}` : problems.map((p) => `${p.name}: ${p.fix}`).join(" "));
  }
  const emails = env.CLOUDFLARE_ACCESS_ALLOWED_EMAILS.split(",").map((s) => s.trim()).filter(Boolean);
  return { account: env.CLOUDFLARE_ACCOUNT_ID || "", host: env.CLOUDFLARE_HOSTNAME.toLowerCase(), emails, teamName: (env.CLOUDFLARE_ACCESS_TEAM_NAME || "").toLowerCase() };
}

// Reports go to the job summary and, for pull requests, a comment file the workflow posts. Nothing personal is written:
// the hostname and email addresses are replaced before anything leaves this process.
function makeReport(env, writeFile = writeFileSync) {
  return (md, { comment = true } = {}) => {
    if (env.GITHUB_STEP_SUMMARY) appendFileSync(env.GITHUB_STEP_SUMMARY, `${md}\n`);
    if (comment && env.COMMENT_FILE) writeFile(env.COMMENT_FILE, `<!-- dashboard-deploy-report -->\n${md}\n`);
  };
}
export function redact(text, c) {
  let out = text;
  for (const v of [c.host, ...c.emails].filter(Boolean)) out = out.split(v).join("***");
  return out;
}

const settingsUrl = (env, kind) => `https://github.com/${env.GITHUB_REPOSITORY || "<owner>/<repo>"}/settings/${kind === "secret" ? "secrets" : "variables"}/actions/new`;

/** Step 1 of CI: which settings are missing or malformed, and exactly where to set them. Throws if any. */
export function preflightNames(env, report = makeReport(env)) {
  const problems = envProblems(env);
  if (env.PF_CONFIG_ERROR) problems.unshift({ name: "OP_VAULT", kind: "conflict", fix: env.PF_CONFIG_ERROR });
  const op = secretsMode(env) === "onepassword" && !env.PF_CONFIG_ERROR;
  const mode = op ? `1Password (OP_VAULT=${env.OP_VAULT}, read by op run from app/1password.env)` : "environment variables, GitHub secrets in CI (OP_VAULT not set)";
  const rows = SETTINGS.map(({ name, kind }) => `| \`${name}\` | ${op ? "1Password item" : kind} | ${problems.some((p) => p.name === name) ? "❌ missing or invalid" : "✅ set"} |`);
  const head = `Settings source: **${mode}**.\n\n`;
  if (!problems.length) {
    report(`### Preflight: settings\n\n${head}| Name | Where | Status |\n| --- | --- | --- |\n${rows.join("\n")}`, { comment: false });
    return;
  }
  const where = (p) => (["environment", "conflict"].includes(p.kind) ? "" : ` [Set it here](${settingsUrl(env, p.kind)}).`);
  const fixes = problems.map((p) => `- **\`${p.name}\`** (${p.kind}): ${p.fix}${where(p)}`);
  const md = `### ❌ Dashboard deploy is not configured\n\nThis run stopped before touching Cloudflare. ${head}Fix these, then re-run the workflow:\n\n${fixes.join("\n")}\n\n| Name | Where | Status |\n| --- | --- | --- |\n${rows.join("\n")}\n\nFull steps: app/DEPLOY.md.`;
  report(md);
  for (const p of problems) console.log(`::error title=${p.kind === "conflict" ? "Two settings sources" : `Missing ${p.kind}`} ${p.name}::${p.fix}${["environment", "conflict"].includes(p.kind) ? "" : ` Set it at ${settingsUrl(env, p.kind)}`}`);
  throw new DeployError(`Not configured: ${problems.map((p) => p.name).join(", ")}`);
}

const PROBES = [
  ["Workers Scripts: Edit (account)", (a) => `/accounts/${a}/workers/scripts`],
  ["Workers R2 Storage: Edit (account). R2 may also need enabling: Dashboard, R2 Object Storage, free plan", (a) => `/accounts/${a}/r2/buckets`],
  ["Access: Apps and Policies: Edit (account). Zero Trust may also need enabling: Dashboard, Zero Trust, Free plan", (a) => `/accounts/${a}/access/apps?per_page=1`],
  ["Workers Routes: Edit (zone)", (a, z) => `/zones/${z}/workers/routes`],
  ["DNS: Edit (zone)", (a, z) => `/zones/${z}/dns_records?per_page=1`],
];

/** Step 2 of CI: the token is live, sees the zone, and can reach every API the deploy uses. Read-only probes. */
export async function preflightToken(env, { fetchFn = fetch, report = makeReport(env) } = {}) {
  const api = makeApi(env, fetchFn);
  const c = { host: (env.CLOUDFLARE_HOSTNAME || "").toLowerCase(), emails: [] };
  const problems = []; const warnings = []; const oks = [];
  const fail = (m) => problems.push(m);
  let zone = null;
  try { zone = await findZone(api, c.host); oks.push(`Zone found for the hostname (token has Zone: Read)`); } catch (e) { fail(`${e.message}`); }
  let verified = false;
  try { verified = (await api("/user/tokens/verify")).status === "active"; } catch { /* account-owned tokens verify under the account */ }
  const account = env.CLOUDFLARE_ACCOUNT_ID || zone?.account || "";
  if (!verified && account) { try { verified = (await api(`/accounts/${account}/tokens/verify`)).status === "active"; } catch { /* handled below */ } }
  if (!verified && !problems.length) fail("The Cloudflare API rejected CLOUDFLARE_API_TOKEN (invalid, expired or revoked). Create a new one (app/DEPLOY.md step 1).");
  if (zone && env.CLOUDFLARE_ACCOUNT_ID && zone.account && env.CLOUDFLARE_ACCOUNT_ID !== zone.account) fail(`CLOUDFLARE_ACCOUNT_ID does not own the zone ${zone.name}. Delete that variable to use the zone's account.`);
  if (zone && account && !problems.length) {
    for (const [what, path] of PROBES) {
      try { await api(path(account, zone.id)); oks.push(what.split(".")[0]); } catch (e) { fail(`Token cannot use ${what}. Cloudflare said: ${e.message}`); }
    }
    try { await api(`/accounts/${account}/access/organizations`); oks.push("Zero Trust team exists (it will be reused)"); } catch (e) {
      if (e.status === 404) oks.push("No Zero Trust team yet (OpenTofu will create one)");
      else fail(`Token cannot read the Zero Trust organization (needs Access: Organizations, Identity Providers, and Groups: Edit; Zero Trust may also need enabling). Cloudflare said: ${e.message}`);
    }
    try {
      const attached = await api(`/accounts/${account}/workers/domains?hostname=${encodeURIComponent(c.host)}`);
      if (attached?.length) oks.push(`Hostname already attached to Worker "${attached[0].service}" (will be updated in place)`);
      else if ((await api(`/zones/${zone.id}/dns_records?name=${encodeURIComponent(c.host)}`))?.length) warnings.push(`A DNS record already exists for the hostname and no Worker owns it. Attaching the Worker will fail until you delete that record in the Cloudflare DNS page, or choose another hostname.`);
    } catch { /* already reported by the probes above */ }
  }
  const lines = [...problems.map((m) => `- ❌ ${m}`), ...warnings.map((m) => `- ⚠️ ${m}`), ...oks.map((m) => `- ✅ ${m}`)];
  if (problems.length) {
    report(`### ❌ Cloudflare token check failed\n\nThis run stopped before changing anything. Edit the token at https://dash.cloudflare.com/profile/api-tokens (add the permissions listed, keep the same secret value), then re-run.\n\n${lines.join("\n")}`);
    for (const m of problems) console.log(`::error title=Cloudflare token::${m.replace(/\n/g, " ")}`);
    throw new DeployError(`Token check failed (${problems.length} problem${problems.length > 1 ? "s" : ""})`);
  }
  report(`### Preflight: Cloudflare token\n\n${lines.join("\n")}`, { comment: false });
  return { warnings };
}

export function makeApi(env, fetchFn = fetch) {
  return async (path) => {
    const res = await fetchFn(`https://api.cloudflare.com/client/v4${path}`, { headers: { Authorization: `Bearer ${env.CLOUDFLARE_API_TOKEN}` } });
    let json = {};
    try { json = await res.json(); } catch { /* non-JSON body */ }
    if (!res.ok || json.success === false) {
      const msg = (json.errors || []).map((e) => e.message).join("; ") || `HTTP ${res.status}`;
      const err = new DeployError(`Cloudflare API GET ${path} failed: ${msg}`);
      err.status = res.status;
      throw err;
    }
    return json.result;
  };
}

/** The zone that serves the hostname: the longest suffix of it that is a zone the token can see. */
export async function findZone(api, host) {
  const labels = host.split(".");
  for (let i = 0; i < labels.length - 1; i++) {
    const name = labels.slice(i).join(".");
    const [zone] = await api(`/zones?name=${encodeURIComponent(name)}`);
    if (zone) return { id: zone.id || "", name: zone.name, account: zone.account?.id || "" };
  }
  throw new DeployError(`No Cloudflare zone found for ${host}. Its domain must already be in the Cloudflare account, and the API token needs Zone: Read on it.`);
}

/** R2's S3 credentials for an API token: access key id = the token's id, secret = SHA-256 hex of the token value. */
export async function r2Credentials(api, env, account) {
  let id;
  try { id = (await api("/user/tokens/verify")).id; } catch { id = (await api(`/accounts/${account}/tokens/verify`)).id; }
  return { AWS_ACCESS_KEY_ID: id, AWS_SECRET_ACCESS_KEY: createHash("sha256").update(env.CLOUDFLARE_API_TOKEN).digest("hex") };
}

/** Pure: the Workers runtime date from wrangler.toml, so local dev (wrangler dev) and the deployed Worker match. */
export function compatibilityDate(template) {
  const m = template.match(/^compatibility_date\s*=\s*"(\d{4}-\d{2}-\d{2})"/m);
  if (!m) throw new DeployError("wrangler.toml has no compatibility_date");
  return m[1];
}

/** Addresses a saved plan would delete or replace. */
export function planDestroys(planJson) {
  try { return (JSON.parse(planJson).resource_changes || []).filter((r) => r.change?.actions?.includes("delete")).map((r) => r.address); } catch { return ["(unreadable plan)"]; }
}

/** Newest modification time under a directory (recursive), or 0 when it does not exist. */
function newest(dir, keep = () => true) {
  if (!existsSync(dir)) return 0;
  return Math.max(0, ...readdirSync(dir, { withFileTypes: true }).map((e) => {
    const p = join(dir, e.name);
    return e.isDirectory() ? newest(p, keep) : keep(e.name) ? statSync(p).mtimeMs : 0;
  }));
}
const siteReady = () => existsSync(join(SITE, "index.html"));

export async function run({ env, fetchFn = fetch, exec = defaultExec, log = console.log, apply = false, dryRun = false, report = makeReport(env),
  readTemplate = () => readFileSync(join(APP, "wrangler.toml"), "utf8"), writeFile = writeFileSync, removeFile = (p) => rmSync(p, { force: true }),
  hasSite = siteReady, planFresh = () => existsSync(PLAN) && statSync(PLAN).mtimeMs >= Math.max(newest(INFRA, (f) => f.endsWith(".tf")), newest(SITE)) }) {
  const c = checkEnv(env);
  const api = makeApi(env, fetchFn);
  const notes = [];
  const note = (m) => { notes.push(m); log(m); };
  if (!hasSite()) throw new DeployError("site/index.html is missing. Build the site first: python tools/build_report.py --out site (mise run site).");

  // 0. The hostname's zone must exist; it also tells us the account and a default team name, so neither is asked for.
  const zone = await findZone(api, c.host);
  if (c.account && zone.account && c.account !== zone.account) throw new DeployError(`CLOUDFLARE_ACCOUNT_ID does not own the zone ${zone.name}. Remove the variable to use the zone's account.`);
  c.account = c.account || zone.account;
  if (!c.account) throw new DeployError("Could not read the account id from the zone. Set CLOUDFLARE_ACCOUNT_ID.");
  env = { ...env, CLOUDFLARE_ACCOUNT_ID: c.account };
  const defaultTeam = zone.name.split(".")[0].replace(/[^a-z0-9-]/g, "").slice(0, 32);

  // 1. Zero Trust organization: use the existing team, or have OpenTofu create one.
  const org = await api(`/accounts/${c.account}/access/organizations`).catch((e) => (e.status === 404 ? null : Promise.reject(e)));
  const existingDomain = org?.auth_domain || "";

  // 2. State bucket and the S3 credentials derived from the API token. The bucket is OpenTofu's (infra/bootstrap): when it
  // is missing, the bootstrap stack creates it with local state, then moves that state into the bucket it created.
  const creds = await r2Credentials(api, env, c.account);
  const tfEnv = { ...env, ...creds, AWS_ENDPOINT_URL_S3: `https://${c.account}.r2.cloudflarestorage.com`, AWS_REGION: "auto", TF_IN_AUTOMATION: "1" };
  const tofu = (args, opts = {}) => exec("tofu", [`-chdir=${INFRA}`, ...args], { env: tfEnv, ...opts });
  const hasBucket = Boolean(await api(`/accounts/${c.account}/r2/buckets/${STATE_BUCKET}`).catch((e) => (e.status === 404 ? null : Promise.reject(e))));
  if (hasBucket) log(`ok: state bucket ${STATE_BUCKET} exists`);
  else if (dryRun) note(`NOTE: first deploy. The state bucket ${STATE_BUCKET} does not exist yet; this dry run creates nothing and plans against empty state. The deploy creates it.`);
  else {
    note(`${apply ? "DO" : "NOTE"}: creating the OpenTofu state bucket ${STATE_BUCKET} with infra/bootstrap (empty, private, needed before planning)`);
    const boot = (args) => exec("tofu", [`-chdir=${BOOTSTRAP}`, ...args], { env: { ...tfEnv, TF_VAR_account_id: c.account, TF_VAR_state_bucket: STATE_BUCKET } });
    writeFile(BOOT_OVERRIDE, `terraform {\n  backend "local" {\n    path = "${BOOT_LOCAL_STATE}"\n  }\n}\n`);
    try {
      boot(["init", "-input=false", "-reconfigure"]);
      boot(["apply", "-input=false", "-auto-approve"]);
    } finally { removeFile(BOOT_OVERRIDE); }
    boot(["init", "-input=false", "-migrate-state", "-force-copy", `-backend-config=bucket=${STATE_BUCKET}`]);
    removeFile(BOOT_LOCAL_STATE);
  }
  const localState = dryRun && !hasBucket;
  const init = () => tofu(["init", "-input=false", ...(localState ? ["-backend=false"] : [`-backend-config=bucket=${STATE_BUCKET}`])]);

  // 3. Decide who manages the organization: OpenTofu if it created it earlier (it is in state), or if none exists yet.
  init();
  const stateAddrs = localState ? [] : tofu(["state", "list"], { capture: true }).split("\n").map((l) => l.trim());
  const inState = stateAddrs.includes(ORG_ADDR);
  const manageOrg = inState || !existingDomain;
  if (manageOrg && !c.teamName) {
    c.teamName = defaultTeam;
    if (!inState) note(`NOTE: this account has no Zero Trust team; OpenTofu will create ${c.teamName}.cloudflareaccess.com (set CLOUDFLARE_ACCESS_TEAM_NAME to choose another name)`);
  }
  const template = readTemplate();
  const vars = { account_id: c.account, hostname: c.host, allowed_emails: JSON.stringify(c.emails), manage_org: String(manageOrg), team_name: c.teamName, team_domain: existingDomain,
    zone_id: zone.id, compatibility_date: compatibilityDate(template) };
  Object.assign(tfEnv, Object.fromEntries(Object.entries(vars).map(([k, v]) => [`TF_VAR_${k}`, v])));

  if (!apply) {
    // Validates wrangler.toml and the site/ folder the way wrangler would upload them (builds only, deploys nothing).
    exec("npx", ["wrangler", "deploy", "--dry-run", "--outdir", "dist"], { cwd: APP, env: { ...env, WRANGLER_SEND_METRICS: "false" } });
    tofu(["validate"]);
    tofu(["plan", "-input=false", ...(dryRun ? ["-lock=false"] : []), `-out=${PLAN}`]);
    const text = redact(tofu(["show", "-no-color", PLAN], { capture: true }), c);
    const destroys = planDestroys(tofu(["show", "-json", PLAN], { capture: true }));
    if (destroys.length) note(`WARNING: this plan would delete or replace: ${destroys.join(", ")}. Auto-apply refuses plans like this.`);
    const mode = dryRun ? "Dry run (nothing is changed)" : "Plan";
    report(`### OpenTofu ${mode}\n\n${notes.map((n) => `- ${redact(n, c)}`).join("\n") || "- No notes."}\n\n<details open><summary>Plan</summary>\n\n\`\`\`\n${text.length > 50000 ? `${text.slice(0, 50000)}\n... truncated, see the job log` : text}\n\`\`\`\n</details>`);
    log(dryRun ? "DRY RUN: nothing was created or changed." : "PLAN ONLY: nothing but the empty state bucket was created. Review the plan above, then run with --apply.");
    return { applied: false, notes, destroys };
  }
  if (!planFresh()) throw new DeployError("No current saved plan. Run the plan first (and rerun it after any change to infra/*.tf or site/), then --apply.");
  const destroys = planDestroys(tofu(["show", "-json", PLAN], { capture: true }));
  if (destroys.length && env.TOFU_ALLOW_DESTROY !== "1") throw new DeployError(`Refusing to apply: the plan would delete or replace ${destroys.join(", ")}. Review it, then run locally with TOFU_ALLOW_DESTROY=1 if that is intended.`);
  tofu(["apply", "-input=false", PLAN]);

  // 4. Smoke test: an unauthenticated request must be stopped by Access, never served.
  const res = await fetchFn(`https://${c.host}/`, { redirect: "manual" });
  const loc = res.headers.get("location") || "";
  const blocked = (res.status >= 300 && res.status < 400 && loc.includes("cloudflareaccess.com")) || res.status === 401 || res.status === 403;
  if (!blocked) throw new DeployError(`SECURITY: unauthenticated request to https://${c.host}/ returned ${res.status}. Do not use the dashboard until this is fixed.`);
  log(`ok: unauthenticated request is blocked (${res.status})`);
  return { applied: true, host: c.host };
}

function defaultExec(cmd, args, { cwd, env, capture, input } = {}) {
  const r = spawnSync(cmd, args, { cwd, env, input, encoding: "utf8", stdio: [input === undefined ? "ignore" : "pipe", capture ? "pipe" : "inherit", "inherit"] });
  if (r.error?.code === "ENOENT") throw new DeployError(`${cmd} is not installed (see app/DEPLOY.md)`);
  if (r.status !== 0) throw new DeployError(`${cmd} ${args.filter((a) => !a.startsWith("-chdir")).slice(0, 3).join(" ")} failed`);
  return r.stdout;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const argv = process.argv.slice(2);
  const main = async () => {
    // 1Password mode: the values arrive inside op run, after the workflow's own masking step, and op masks only whole
    // values. Mask the hostname and each address on its own so none shows up in the log.
    if (process.env.GITHUB_ACTIONS && process.env.OP_VAULT) {
      for (const v of [process.env.CLOUDFLARE_HOSTNAME, ...(process.env.CLOUDFLARE_ACCESS_ALLOWED_EMAILS || "").split(",")].map((x) => x?.trim()).filter(Boolean)) console.log(`::add-mask::${v}`);
    }
    if (argv.includes("--preflight-names")) return preflightNames(process.env);
    if (argv.includes("--preflight-token")) return preflightToken(process.env);
    return run({ env: process.env, apply: argv.includes("--apply"), dryRun: argv.includes("--dry-run") });
  };
  main().catch((e) => {
    console.error(e instanceof DeployError ? `error: ${e.message}` : e);
    process.exit(1);
  });
}
