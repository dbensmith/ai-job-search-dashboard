import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { checkEnv, compatibilityDate, run, DeployError, STATE_BUCKET, envProblems, secretsMode, preflightNames, preflightToken, planDestroys, redact } from "../deploy/deploy.mjs";

// Shared fixture values: change them here, not in each test.
const HOST = "dashboard.example.com", ZONE = "example.com", TEAM = "example", EMAIL = "me@example.com";
const env = { CLOUDFLARE_API_TOKEN: "SECRET-TOKEN-123", CLOUDFLARE_ACCOUNT_ID: "acct", CLOUDFLARE_HOSTNAME: HOST.replace("dashboard", "Dashboard"), CLOUDFLARE_ACCESS_ALLOWED_EMAILS: `${EMAIL}, you@example.com` };
const template = readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8");

// Fake Cloudflare API + site. orgDomain: existing Zero Trust auth domain, or null for none.
function fetcher({ orgDomain = "team.cloudflareaccess.com", siteStatus = 302, zone = ZONE, zoneAccount = "acct", fail = [], domains = [], dns = [], bucket = true } = {}) {
  return async (url) => {
    const u = String(url);
    const ok = (result) => new Response(JSON.stringify({ success: true, result }), { status: 200 });
    const denied = (m = "forbidden") => new Response(JSON.stringify({ success: false, errors: [{ message: m }] }), { status: 403 });
    if (fail.some((f) => u.includes(f))) return denied("Authentication error");
    if (u.endsWith("/access/organizations")) return orgDomain ? ok({ auth_domain: orgDomain }) : new Response(JSON.stringify({ success: false, errors: [{ message: "not found" }] }), { status: 404 });
    if (u.includes("/zones?name=")) return ok(u.endsWith(`=${zone}`) ? [{ id: "zone-id", name: zone, account: { id: zoneAccount } }] : []);
    if (u.endsWith("/user/tokens/verify")) return ok({ id: "token-id-1", status: "active" });
    if (u.includes("/workers/domains")) return ok(domains);
    if (u.includes("/dns_records")) return ok(dns);
    if (u.endsWith(`/r2/buckets/${STATE_BUCKET}`)) return bucket ? ok({ name: STATE_BUCKET }) : new Response(JSON.stringify({ success: false, errors: [{ message: "The specified bucket does not exist." }] }), { status: 404 });
    if (/\/(workers\/scripts|r2\/buckets|workers\/routes|access\/apps)/.test(u)) return ok([]);
    return new Response("", { status: siteStatus, headers: { location: "https://team.cloudflareaccess.com/login" } });
  };
}

function harness({ destroys = [], state = "", teamDomain = "team.cloudflareaccess.com" } = {}) {
  const cmds = []; const files = {};
  const exec = (cmd, args, opts = {}) => {
    cmds.push({ line: [cmd, ...args].join(" "), env: opts.env });
    if (args.includes("state")) return state;
    if (args.includes("show") && args.includes("-json")) return JSON.stringify({ resource_changes: destroys.map((address) => ({ address, change: { actions: ["delete"] } })) });
    if (args.includes("show")) return `Plan for ${HOST} allowing ${EMAIL}`;
    return "";
  };
  const removed = [];
  return { cmds, files, removed, exec, writeFile: (p, t) => { files[p] = t; }, removeFile: (p) => { removed.push(p); } };
}
const base = (h, extra = {}) => ({ env, log: () => {}, exec: h.exec, writeFile: h.writeFile, removeFile: h.removeFile, readTemplate: () => template, fetchFn: fetcher(), hasSite: () => true, planFresh: () => true, ...extra });
const tofuCmds = (h) => h.cmds.filter((c) => c.line.startsWith("tofu"));
const t0 = (h) => tofuCmds(h).at(-1).env;

test("environment is validated", () => {
  assert.throws(() => checkEnv({}), /Missing environment variables/);
  assert.throws(() => checkEnv({ ...env, CLOUDFLARE_HOSTNAME: "nope" }), DeployError);
  assert.throws(() => checkEnv({ ...env, CLOUDFLARE_ACCESS_ALLOWED_EMAILS: "not-an-email" }), DeployError);
  assert.throws(() => checkEnv({ ...env, CLOUDFLARE_ACCESS_TEAM_NAME: "Bad Name!" }), DeployError);
  assert.equal(checkEnv(env).host, HOST);
});

test("plan mode: existing bucket and team, only init/validate/plan", async () => {
  const h = harness(); const logs = [];
  const r = await run(base(h, { log: (m) => logs.push(m) }));
  assert.equal(r.applied, false);
  assert.ok(!h.cmds.some((c) => /bucket create|apply|wrangler deploy(?! --dry-run)/.test(c.line)));
  assert.ok(h.cmds.some((c) => c.line.includes("wrangler deploy --dry-run --outdir dist")), "validates the static bundle before planning");
  assert.equal(t0(h).TF_VAR_zone_id, "zone-id");
  assert.equal(t0(h).TF_VAR_compatibility_date, compatibilityDate(template));
  const t = tofuCmds(h);
  assert.ok(t[0].line.includes(" init ") && t[0].line.includes(`-backend-config=bucket=${STATE_BUCKET}`));
  assert.equal(t[0].env.AWS_ENDPOINT_URL_S3, "https://acct.r2.cloudflarestorage.com");
  assert.equal(t[0].env.AWS_ACCESS_KEY_ID, "token-id-1");
  assert.equal(t[0].env.AWS_SECRET_ACCESS_KEY, createHash("sha256").update("SECRET-TOKEN-123").digest("hex"));
  assert.ok(t.some((c) => c.line.includes(" validate")) && t.some((c) => c.line.includes(" plan ")));
  assert.equal(t.at(-1).env.TF_VAR_manage_org, "false");
  assert.equal(t.at(-1).env.TF_VAR_team_domain, "team.cloudflareaccess.com");
  assert.deepEqual(Object.keys(h.files), [], "plan writes no files");
  assert.ok(!logs.join("\n").includes("SECRET-TOKEN"));
});

test("missing state bucket: the bootstrap stack creates it with local state, then moves that state into it", async () => {
  const h = harness();
  await run(base(h, { fetchFn: fetcher({ bucket: false }) }));
  const boot = h.cmds.filter((c) => c.line.includes("/infra/bootstrap "));
  assert.deepEqual(boot.map((c) => c.line.split("/infra/bootstrap ")[1]), [
    "init -input=false -reconfigure", "apply -input=false -auto-approve",
    `init -input=false -migrate-state -force-copy -backend-config=bucket=${STATE_BUCKET}`]);
  assert.equal(boot[1].env.TF_VAR_account_id, "acct");
  assert.equal(boot[1].env.AWS_ACCESS_KEY_ID, "token-id-1");
  const override = Object.entries(h.files).find(([p]) => p.endsWith("bootstrap/backend_override.tf"));
  assert.match(override[1], /backend "local"/);
  assert.deepEqual(h.removed.map((p) => p.split("/").slice(-2).join("/")), ["bootstrap/backend_override.tf", "bootstrap/bootstrap.tfstate"]);
  assert.ok(!h.cmds.some((c) => /wrangler r2/.test(c.line)), "wrangler no longer touches the bucket");
  const main = tofuCmds(h).find((c) => !c.line.includes("/bootstrap") && c.line.includes(" init "));
  assert.ok(main.line.includes(`-backend-config=bucket=${STATE_BUCKET}`));
});

test("existing state bucket: the bootstrap stack does not run", async () => {
  const h = harness();
  await run(base(h));
  assert.ok(!h.cmds.some((c) => c.line.includes("/bootstrap")));
});

test("account id and team name default from the hostname's zone", async () => {
  const h = harness(); const { CLOUDFLARE_ACCOUNT_ID, ...bare } = env;
  await run(base(h, { env: bare, fetchFn: fetcher({ orgDomain: null, zoneAccount: "zone-acct" }) }));
  const last = tofuCmds(h).at(-1).env;
  assert.equal(last.TF_VAR_account_id, "zone-acct");
  assert.equal(last.AWS_ENDPOINT_URL_S3, "https://zone-acct.r2.cloudflarestorage.com");
  assert.equal(last.TF_VAR_team_name, TEAM);
  assert.equal(h.cmds.find((c) => c.line.includes("wrangler")).env.CLOUDFLARE_ACCOUNT_ID, "zone-acct");
});

test("hostname without a zone, or an account that does not own it, stops before anything runs", async () => {
  const h = harness();
  await assert.rejects(run(base(h, { fetchFn: fetcher({ zone: "other.net" }) })), /No Cloudflare zone/);
  await assert.rejects(run(base(h, { fetchFn: fetcher({ zoneAccount: "someone-else" }) })), /does not own/);
  assert.equal(h.cmds.length, 0);
});

test("no Zero Trust team: OpenTofu creates the named one", async () => {
  const h2 = harness();
  await run(base(h2, { fetchFn: fetcher({ orgDomain: null }), env: { ...env, CLOUDFLARE_ACCESS_TEAM_NAME: "Benj" } }));
  const last = tofuCmds(h2).at(-1).env;
  assert.equal(last.TF_VAR_manage_org, "true");
  assert.equal(last.TF_VAR_team_name, "benj");
});

test("team already created by OpenTofu stays managed on later runs", async () => {
  const h = harness({ state: "cloudflare_zero_trust_organization.org\ncloudflare_workers_script.dashboard\n" });
  await run(base(h));
  assert.equal(tofuCmds(h).at(-1).env.TF_VAR_manage_org, "true");
});

test("apply refuses without a fresh saved plan", async () => {
  const h = harness();
  await assert.rejects(run(base(h, { apply: true, planFresh: () => false })), /saved plan/);
  assert.ok(!h.cmds.some((c) => /apply|deploy/.test(c.line)));
});

test("apply: applies exactly the saved plan, with the settings as variables, and never runs wrangler deploy", async () => {
  const h = harness();
  const r = await run(base(h, { apply: true }));
  assert.equal(r.applied, true);
  const lines = h.cmds.map((c) => c.line);
  assert.ok(lines.some((l) => / apply -input=false .*dashboard\.tfplan$/.test(l)), lines.join("\n"));
  const t = tofuCmds(h)[0].env;
  assert.equal(t.TF_VAR_hostname, HOST);
  assert.equal(t.TF_VAR_allowed_emails, '["me@example.com","you@example.com"]');
  assert.ok(!lines.some((l) => /wrangler/.test(l)), "the apply step never runs wrangler");
});

test("the static site must be built before any plan or apply", async () => {
  const h = harness();
  await assert.rejects(run(base(h, { hasSite: () => false })), /build_report/);
  assert.equal(h.cmds.length, 0);
});

test("fails loudly if the site is served without Access", async () => {
  const h = harness();
  await assert.rejects(run(base(h, { apply: true, fetchFn: fetcher({ siteStatus: 200 }) })), /SECURITY/);
});

test("wrangler.toml has a compatibility date and serves the static site folder", () => {
  assert.match(compatibilityDate(template), /^\d{4}-\d{2}-\d{2}$/);
  assert.match(template, /directory = "\.\.\/site"/);
  assert.doesNotMatch(template, /d1_databases|migrations_dir|main\s*=/);
});

const quiet = () => {};
const reports = () => { const r = []; return { r, report: (m) => r.push(m) }; };

test("every missing or invalid setting is listed at once, with where to set it", () => {
  const p = envProblems({ CLOUDFLARE_HOSTNAME: "https://x", CLOUDFLARE_ACCESS_ALLOWED_EMAILS: "" });
  assert.deepEqual(p.map((x) => x.name), ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_HOSTNAME", "CLOUDFLARE_ACCESS_ALLOWED_EMAILS"]);
  assert.deepEqual(envProblems(env), []);
});

test("mode selection: OP_VAULT switches to 1Password; unset is plain environment variables", () => {
  assert.equal(secretsMode({}), "env");
  assert.equal(secretsMode({ CLOUDFLARE_API_TOKEN: "x", OP_VAULT: "" }), "env");
  assert.equal(secretsMode({ OP_VAULT: "ops" }), "onepassword");
});

test("1Password mode: values arrive loaded by op run; a missing one is fixed in the item, not GitHub", () => {
  const loaded = { OP_VAULT: "ops", ...env };
  assert.deepEqual(envProblems(loaded), []);
  const p = envProblems({ ...loaded, CLOUDFLARE_HOSTNAME: "https://x", CLOUDFLARE_API_TOKEN: "" });
  assert.deepEqual(p.map((x) => x.name), ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_HOSTNAME"]);
  assert.ok(p.every((x) => x.kind === "environment" && /item ai-job-search \(vault ops\)/.test(x.fix)));
});

test("preflight names report which mode is active and which names are absent in it", () => {
  const gh = reports();
  assert.throws(() => preflightNames({ GITHUB_REPOSITORY: "me/repo" }, gh.report), /CLOUDFLARE_API_TOKEN/);
  assert.match(gh.r[0], /OP_VAULT not set/);
  assert.match(gh.r[0], /settings\/secrets\/actions\/new/);
  const op = reports();
  assert.throws(() => preflightNames({ OP_VAULT: "ops", GITHUB_REPOSITORY: "me/repo", ...env, CLOUDFLARE_HOSTNAME: "" }, op.report), /CLOUDFLARE_HOSTNAME/);
  assert.match(op.r[0], /1Password \(OP_VAULT=ops/);
  assert.doesNotMatch(op.r[0], /actions\/new/); // nothing to set in GitHub
  const mixed = reports(); // the loader found both sources and handed the names preflight its message
  assert.throws(() => preflightNames({ OP_VAULT: "ops", PF_CONFIG_ERROR: "OP_VAULT is set (1Password), but CLOUDFLARE_API_TOKEN is also set", ...env }, mixed.report), /OP_VAULT/);
  assert.match(mixed.r[0], /also set/);
});

test("preflight names: fails with a report naming each absent name and its settings link", () => {
  const { r, report } = reports();
  assert.throws(() => preflightNames({ GITHUB_REPOSITORY: "me/repo" }, report), /CLOUDFLARE_API_TOKEN, CLOUDFLARE_HOSTNAME, CLOUDFLARE_ACCESS_ALLOWED_EMAILS/);
  assert.match(r[0], /github\.com\/me\/repo\/settings\/secrets\/actions\/new/);
  assert.match(r[0], /❌ missing/);
  const ok = reports();
  preflightNames(env, ok.report); // no source set: env is the default
  assert.match(ok.r[0], /✅ set/);
});

test("preflight token: passes, or names each permission the token lacks", async () => {
  const good = reports();
  await preflightToken(env, { fetchFn: fetcher(), report: good.report });
  assert.match(good.r[0], /Zero Trust team exists/);
  const bad = reports();
  await assert.rejects(preflightToken(env, { fetchFn: fetcher({ fail: ["/workers/routes", "/r2/buckets"] }), report: bad.report }), /2 problems/);
  assert.match(bad.r[0], /Workers Routes: Edit/); assert.match(bad.r[0], /R2 Storage: Edit/);
  const none = reports();
  await assert.rejects(preflightToken(env, { fetchFn: fetcher({ zone: "other.net" }), report: none.report }), DeployError);
  assert.match(none.r[0], /No Cloudflare zone/);
});

test("preflight token: a stray DNS record is a warning, a Worker-owned hostname is fine", async () => {
  const a = reports();
  await preflightToken(env, { fetchFn: fetcher({ dns: [{ type: "A" }] }), report: a.report });
  assert.match(a.r[0], /⚠️ A DNS record already exists/);
  const b = reports();
  await preflightToken(env, { fetchFn: fetcher({ domains: [{ service: "ai-job-search-dashboard" }], dns: [{ type: "AAAA" }] }), report: b.report });
  assert.match(b.r[0], /already attached to Worker "ai-job-search-dashboard"/); assert.doesNotMatch(b.r[0], /⚠️/);
});

test("dry run creates nothing: no state bucket, local state, no lock, and the report hides the hostname and emails", async () => {
  const h = harness(); const { r, report } = reports();
  await run(base(h, { dryRun: true, report, fetchFn: fetcher({ bucket: false }) }));
  assert.ok(!h.cmds.some((c) => /bucket create|apply|wrangler deploy(?! --dry-run)/.test(c.line)));
  const t = tofuCmds(h);
  assert.ok(t[0].line.includes("-backend=false"));
  assert.ok(!t.some((c) => c.line.includes(" state ")));
  assert.ok(t.find((c) => c.line.includes(" plan ")).line.includes("-lock=false"));
  assert.match(r[0], /first deploy/); assert.match(r[0], /Plan for \*\*\* allowing \*\*\*/); assert.doesNotMatch(r[0], /example\.com/);
});

test("apply refuses a plan that deletes anything unless explicitly allowed", async () => {
  const h = harness({ destroys: ["cloudflare_workers_script.dashboard"] });
  await assert.rejects(run(base(h, { apply: true })), /Refusing to apply/);
  assert.ok(!h.cmds.some((c) => / apply /.test(c.line)));
  await run(base(h, { apply: true, env: { ...env, TOFU_ALLOW_DESTROY: "1" } }));
  assert.deepEqual(planDestroys('{"resource_changes":[{"address":"a","change":{"actions":["no-op"]}}]}'), []);
});

test("redact removes hostname and emails", () => {
  assert.equal(redact(`${HOST} ${EMAIL} ok`, { host: HOST, emails: [EMAIL] }), "*** *** ok");
});

// --- local settings loader (deploy/with-settings.mjs) ---
import { parseDotenv, resolveSettings } from "../deploy/with-settings.mjs";

test("parseDotenv reads KEY=VALUE, quotes, export and comments, and executes nothing", () => {
  const out = parseDotenv(`# note\nCLOUDFLARE_API_TOKEN='abc'\nexport CLOUDFLARE_HOSTNAME=${HOST}\n\nX=\$(echo hi)\n  # CLOUDFLARE_ACCESS_ALLOWED_EMAILS=nope\n`);
  assert.deepEqual(out, { CLOUDFLARE_API_TOKEN: "abc", CLOUDFLARE_HOSTNAME: HOST, X: "$(echo hi)" });
});

test("resolveSettings: plain environment without OP_VAULT; environment wins over .env.local", () => {
  const r = resolveSettings({ CLOUDFLARE_HOSTNAME: "env.example.com" }, { CLOUDFLARE_HOSTNAME: "file.example.com", CLOUDFLARE_API_TOKEN: "t" }, { hasOp: false });
  assert.equal(r.error, undefined);
  assert.equal(r.viaOp, false);
  assert.equal(r.child.CLOUDFLARE_HOSTNAME, "env.example.com");
  assert.equal(r.child.CLOUDFLARE_API_TOKEN, "t");
  assert.match(r.source, /app\/\.env or app\/\.env\.local \(OP_VAULT not set\)/);
  assert.deepEqual(r.set.map(([, ok]) => ok), [true, true, false]);
});

test("resolveSettings: OP_VAULT runs op with the committed reference file; nothing else is loaded", () => {
  const r = resolveSettings({ OP_VAULT: "ops" }, {}, { hasOp: true });
  assert.equal(r.viaOp, true);
  assert.deepEqual(r.files, ["1password.env"]);
  assert.match(resolveSettings({ OP_VAULT: "ops" }, {}, { hasOp: false }).error, /mise install/);
  assert.equal(resolveSettings({}, { OP_VAULT: "from-file" }, { hasOp: true }).viaOp, true); // app/.env decides too
});

test("resolveSettings: both sources at once is an error naming each leftover, in env, .env or CI secrets", () => {
  const r = resolveSettings({ OP_VAULT: "ops", CLOUDFLARE_API_TOKEN: "old", CLOUDFLARE_HOSTNAME: "" }, { CLOUDFLARE_ACCESS_ALLOWED_EMAILS: "a@example.com" }, { hasOp: true });
  assert.match(r.error, /CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCESS_ALLOWED_EMAILS are also set/);
  assert.doesNotMatch(r.error, /CLOUDFLARE_HOSTNAME/); // an empty CI secret counts as unset
  assert.ok(r.child); // the names preflight can still run and report it
});
