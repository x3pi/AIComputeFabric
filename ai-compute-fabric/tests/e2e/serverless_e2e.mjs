// Phase 6 Browser E2E test: Real headless Chromium drives the Serverless Admin UI
// on index.html and tenants.html across 2560x1440 and 375x812 viewports and RBAC roles (admin vs viewer).
// Usage:
//   node tests/e2e/serverless_e2e.mjs --gateway /tmp/aicf-site-gateway --loadgen /tmp/aicf-loadgen --out benchmarks/reports/p_serverless_6_e2e.json
import { chromium } from "playwright-core";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, chmodSync, existsSync, readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const arg = n => {
  const i = process.argv.indexOf("--" + n);
  return i >= 0 ? process.argv[i + 1] : "";
};
const GW = arg("gateway") || "/tmp/aicf-site-gateway";
const LG = arg("loadgen") || "/tmp/aicf-loadgen";
const OUT = arg("out") || resolve("benchmarks/reports/p_serverless_6_e2e.json");
const PY = resolve(".venv/bin/python");

const GRPC_PORT = 17964;
const HTTP_PORT = 18694;
const SL_PORT = 18194;
const BASE = `http://127.0.0.1:${HTTP_PORT}`;
const SL_SECRET = "e2e-sl-proxy-secret-p6";
const CHROME = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"].find(existsSync);

const sha = t => createHash("sha256").update(t).digest("hex");
const checks = [];
const check = (name, ok, detail = "") => {
  checks.push({ name, ok: !!ok, detail });
  console.log((ok ? "PASS " : "FAIL ") + name + (detail ? " — " + detail : ""));
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const procs = [];
const dir = mkdtempSync(join(tmpdir(), "aicf-sl-e2e-"));
const rolesFile = join(dir, "roles.json");
const tenantsFile = join(dir, "tenants.json");
const secretFile = join(dir, "sl.secret");
const auditFile = join(dir, "sl_audit.jsonl");

writeFileSync(secretFile, SL_SECRET + "\n");
chmodSync(secretFile, 0o600);

const TOK = { viewer: "tok-sl-viewer", operator: "tok-sl-operator", admin: "tok-sl-admin" };
writeFileSync(rolesFile, JSON.stringify([
  { name: "vera", role: "viewer", token_sha256: sha(TOK.viewer) },
  { name: "omar", role: "operator", token_sha256: sha(TOK.operator) },
  { name: "ada", role: "admin", token_sha256: sha(TOK.admin) },
]));

async function api(path, token = TOK.admin, init = {}) {
  return fetch(BASE + path, {
    ...init,
    headers: { Authorization: "Bearer " + token, ...(init.headers || {}) },
  });
}

async function openRolePage(browser, token, viewport = { width: 2560, height: 1440 }) {
  const ctx = await browser.newContext({ viewport });
  await ctx.addInitScript(t => {
    try { sessionStorage.setItem("aicf_admin", t); } catch {}
  }, token);
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  page.on("console", m => {
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text());
  });
  page.on("dialog", d => d.accept());
  return { page, ctx, errors };
}

try {
  let gwBin = GW;
  let lgBin = LG;
  if (!existsSync(gwBin)) {
    gwBin = join(dir, "site-gateway");
    spawnSync("go", ["build", "-o", gwBin, "."], { cwd: resolve("apps/site-gateway"), stdio: "inherit" });
  }
  if (!existsSync(lgBin)) {
    lgBin = join(dir, "loadgen");
    spawnSync("go", ["build", "-o", lgBin, "./cmd/loadgen"], { cwd: resolve("apps/site-gateway"), stdio: "inherit" });
  }
  // 1. Start Go site-gateway
  procs.push(spawn(gwBin, [
    "-grpc", `127.0.0.1:${GRPC_PORT}`,
    "-http", `127.0.0.1:${HTTP_PORT}`,
    "-roles-file", rolesFile,
    "-tenants-file", tenantsFile,
    "-serverless-url", `http://127.0.0.1:${SL_PORT}`,
    "-serverless-secret-file", secretFile,
  ], { stdio: "ignore" }));
  await sleep(700);

  // 2. Start loadgen so sim workers are online with VRAM
  procs.push(spawn(lgBin, [
    "-grpc", `127.0.0.1:${GRPC_PORT}`,
    "-http", `127.0.0.1:${HTTP_PORT}`,
    "-workers", "2",
    "-sites", "hanoi",
    "-duration", "90s",
  ], { stdio: "ignore" }));
  await sleep(1200);

  // 3. Seed tenant 'alpha' and 'beta' on gateway and capture their generated IDs
  const rAlpha = await api("/api/tenants", TOK.admin, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "alpha", weight: 2, max_inflight: 8 }),
  });
  const dAlpha = await rAlpha.json();
  const alphaId = dAlpha.tenant.id;

  const rBeta = await api("/api/tenants", TOK.admin, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "beta", weight: 1, max_inflight: 4 }),
  });
  const dBeta = await rBeta.json();
  const betaId = dBeta.tenant.id;

  // 4. Write & start Python Serverless HTTP service with seeded warm containers, real lease, and usage
  const pyServerScript = join(dir, "run_sl_service.py");
  writeFileSync(pyServerScript, `
import os, sys, threading, time
sys.path.insert(0, os.getcwd())
from pathlib import Path
from types import SimpleNamespace
from agents.sl_security import new_key_file, load_keys
from serverless.app import serve
from serverless.deployments import DeploymentStore
from serverless.ledger import ServerlessAudit, UsageLedger
from serverless.quota import Quotas, TenantPolicy
from serverless.service import HttpLeaseClient, ServerlessService, WarmEntry
from serverless.volumes import ModelCacheStore, VolumeStore

root = Path(${JSON.stringify(dir)})
kf = root / "hmac.key"
new_key_file(kf)
alpha_id = ${JSON.stringify(alphaId)}
beta_id = ${JSON.stringify(betaId)}

import base64, json
from agents.tasks import TaskResult, TaskType

def _fake_dispatch(task, attempt):
    res_json = base64.b64encode(json.dumps({"release": "e2e-ok", "words": 12}).encode()).decode()
    return TaskResult(
        task_id=task.task_id,
        type=TaskType.SERVERLESS_EXEC,
        output={"status": "ok", "result_b64": res_json, "result_format": "json", "exec_ms": 4.2, "cold_start": False, "worker_id": "sim-00000", "stdout": "[e2e] executed\\n"},
        worker_id="sim-00000",
    )

leases = HttpLeaseClient(${JSON.stringify(BASE)}, ${JSON.stringify(SL_SECRET)})
svc = ServerlessService(
    blobs={},
    quotas=Quotas({
        alpha_id: TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=8192, max_concurrency=4),
        beta_id: TenantPolicy(serverless_enabled=True, gpu_allowed=False, max_vram_mb=2048, max_concurrency=2),
    }, default=TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=4096, max_concurrency=4)),
    keys=load_keys(kf),
    live_workers=lambda: [SimpleNamespace(agent_id="sim-00000", labels=("cpu", "gpu-sl"))],
    dispatch=_fake_dispatch,
    leases=leases,
    volumes=VolumeStore(root / "volumes"),
    model_cache=ModelCacheStore(root / "model_cache"),
    deployments=DeploymentStore(root / "deployments.json"),
    ledger=UsageLedger(root / "usage_ledger.jsonl"),
    audit=ServerlessAudit(Path(${JSON.stringify(auditFile)})),
)

# Record sample usage for alpha_id and beta_id
svc.ledger.record_call(alpha_id, "call-seed-1", 4500, 9216)
svc.ledger.record_idle_lease(alpha_id, "lease-seed-1", 2.0, 2048)
svc.ledger.record_call(beta_id, "call-seed-2", 1200, 1024)

# Acquire a real GPU lease from Go site-gateway for warm container c-e2e-warm-01
lresp = leases.request(idempotency_key="e2e-lease-1", tenant_id=alpha_id, vram_bytes=2048 * 1024 * 1024, ttl_ms=120000)
lease_id = lresp.get("lease_id") or "lease-1"
now = time.time()
svc._warm[(alpha_id, "e2e-app", "infer", "envhash1", 2048)] = WarmEntry(
    worker_id="sim-00000",
    expires=now + 120.0,
    lease_id=lease_id,
    container_id="c-e2e-warm-01",
    tenant_id=alpha_id,
    app_name="e2e-app",
    fn_id="infer",
    vram_mb=2048,
    last_active=now,
)
svc._warm[(alpha_id, "e2e-app", "embed", "envhash2", 1024)] = WarmEntry(
    worker_id="sim-00000",
    expires=now + 180.0,
    lease_id="lease-2",
    container_id="c-e2e-warm-02",
    tenant_id=alpha_id,
    app_name="e2e-app",
    fn_id="embed",
    vram_mb=1024,
    last_active=now,
)

srv = serve(svc, ${JSON.stringify(SL_SECRET)}, "127.0.0.1", ${SL_PORT})
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("SL_READY", flush=True)
while True:
    time.sleep(1)
`);

  const slProc = spawn(PY, [pyServerScript], { stdio: ["ignore", "pipe", "inherit"] });
  procs.push(slProc);
  let slReady = false;
  for (let i = 0; i < 50 && !slReady; i++) {
    try {
      const r = await api("/api/serverless/v1/status", TOK.admin);
      if (r.ok) {
        const d = await r.json();
        slReady = (d.containers || []).length === 2;
      }
    } catch {}
    if (!slReady) await sleep(200);
  }
  check("serverless service & gateway proxy ready with 2 warm containers", slReady);
  if (!slReady) throw new Error("serverless service did not become ready");

  const browser = await chromium.launch({ executablePath: CHROME, headless: true });

  // ---- A. Viewer role checks (must NOT see Force Evict button or global state buttons) ----
  {
    const { page, ctx, errors } = await openRolePage(browser, TOK.viewer, { width: 2560, height: 1440 });
    await page.goto(`${BASE}/index.html`);
    await page.waitForSelector("#serverless-card", { timeout: 15000 });
    await page.evaluate(() => window.AICF_READY);
    await page.waitForFunction(() => document.querySelectorAll("#sl-rows tr[data-cid]").length === 2, null, { timeout: 10000 });

    const evictBtnCount = await page.locator("#sl-rows .sl-evict-btn").count();
    const globalCtrlVisible = await page.evaluate(() => {
      const el = document.getElementById("sl-global-controls");
      return el && getComputedStyle(el).display !== "none";
    });
    check("viewer sees warm containers in #sl-rows but NO Force Evict button", evictBtnCount === 0 && !globalCtrlVisible, `evictBtns=${evictBtnCount}`);

    // Viewer on tenants.html sees Serverless usage table in read-only mode
    await page.goto(`${BASE}/tenants.html`);
    await page.waitForSelector("#sl-tenants-card", { timeout: 10000 });
    await page.evaluate(() => window.AICF_READY);
    await page.waitForFunction(() => document.querySelectorAll("#sl-tenant-rows tr[data-tenant]").length >= 2, null, { timeout: 10000 });
    const saveBtnsDisabled = await page.$$eval("#sl-tenant-rows .sl-pol-save", bs => bs.length >= 2 && bs.every(b => b.disabled));
    check("viewer sees Serverless tenant GPU-seconds table with all quota controls disabled", saveBtnsDisabled);
    check("viewer session had zero JS errors", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }

  // ---- B. Admin role checks at 2560x1440 and 375x812 (Force Evict, Drain/Disable, Tenant Quota Editor) ----
  {
    const { page, ctx, errors } = await openRolePage(browser, TOK.admin, { width: 2560, height: 1440 });
    await page.goto(`${BASE}/index.html`);
    await page.waitForSelector("#serverless-card", { timeout: 15000 });
    await page.evaluate(() => window.AICF_READY);
    await page.waitForFunction(() => document.querySelectorAll("#sl-rows .sl-evict-btn").length === 2, null, { timeout: 10000 });

    // Check 2560x1440 layout: no horizontal overflow & full fluid width
    const wideLayout = await page.evaluate(() => ({
      noOverflow: document.documentElement.scrollWidth <= window.innerWidth,
      mainW: document.querySelector("main").getBoundingClientRect().width,
      vramText: document.getElementById("sl-vram-summary")?.textContent || "",
    }));
    check("index.html at 2560x1440 has no overflow and shows Serverless lease VRAM summary",
      wideLayout.noOverflow && wideLayout.mainW >= 2100 && /3072 MiB/.test(wideLayout.vramText),
      `mainW=${Math.round(wideLayout.mainW)}px, vram=${wideLayout.vramText}`);

    // Switch to 375x812 mobile viewport and verify no horizontal overflow + Force Evict button clickable
    await page.setViewportSize({ width: 375, height: 812 });
    const mobileIndexNoOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    check("index.html at 375x812 mobile adapts cleanly without horizontal overflow", mobileIndexNoOverflow);

    // Click Force Evict on c-e2e-warm-01
    await page.click('#sl-rows .sl-evict-btn[data-cid="c-e2e-warm-01"]');
    await page.waitForFunction(() => document.querySelectorAll("#sl-rows tr[data-cid]").length === 1, null, { timeout: 10000 });
    const remainingCid = await page.getAttribute("#sl-rows tr[data-cid]", "data-cid");
    check("Force Evict removes c-e2e-warm-01 from UI table", remainingCid === "c-e2e-warm-02", `remaining=${remainingCid}`);

    // Verify gateway lease was released
    const activeLeasesRes = await fetch(`${BASE}/api/serverless/v1/_lease/active`, {
      headers: { "X-Aicf-Proxy-Secret": SL_SECRET },
    });
    const activeLeasesDoc = await activeLeasesRes.json();
    check("Force Evict releases the GPU lease on site-gateway", (activeLeasesDoc.leases || []).length === 0, JSON.stringify(activeLeasesDoc));

    // Verify Audit log recorded the admin eviction
    const auditRaw = existsSync(auditFile) ? readFileSync(auditFile, "utf8") : "";
    check("Force Evict writes an audit record with reason=admin",
      auditRaw.includes('"action":"evict"') && auditRaw.includes('"container_id":"c-e2e-warm-01"') && auditRaw.includes('"reason":"admin"'));

    // Toggle Drain & Disable Serverless from UI
    await page.setViewportSize({ width: 2560, height: 1440 });
    await page.click("#sl-btn-toggle-drain");
    await page.waitForFunction(() => /DRAIN/.test(document.getElementById("sl-status-badge")?.textContent || ""), null, { timeout: 5000 });
    let stDoc = await (await api("/api/serverless/v1/status", TOK.admin)).json();
    check("Drain button toggles serverless draining=true", stDoc.draining === true);
    await page.click("#sl-btn-toggle-drain");
    await page.waitForFunction(() => /SẴN SÀNG/.test(document.getElementById("sl-status-badge")?.textContent || ""), null, { timeout: 5000 });

    await page.click("#sl-btn-toggle-enabled");
    await page.waitForFunction(() => /ĐÃ TẮT/.test(document.getElementById("sl-status-badge")?.textContent || ""), null, { timeout: 5000 });
    stDoc = await (await api("/api/serverless/v1/status", TOK.admin)).json();
    check("Disable Serverless button toggles enabled=false", stDoc.enabled === false);
    await page.click("#sl-btn-toggle-enabled");
    await page.waitForFunction(() => /SẴN SÀNG/.test(document.getElementById("sl-status-badge")?.textContent || ""), null, { timeout: 5000 });

    // Navigate to tenants.html and verify GPU-seconds & quota editing at 2560x1440 and 375x812
    await page.goto(`${BASE}/tenants.html`);
    await page.waitForSelector("#sl-tenants-card", { timeout: 10000 });
    await page.waitForFunction(aid => !!document.querySelector(`#sl-tenant-rows tr[data-tenant="${aid}"]`), alphaId, { timeout: 10000 });

    const alphaGpuText = await page.textContent(`#sl-tenant-rows tr[data-tenant="${alphaId}"] .sl-gpu-sec`);
    const alphaVramText = await page.textContent(`#sl-tenant-rows tr[data-tenant="${alphaId}"] .sl-vram-mibs`);
    const vramMibS = parseInt(alphaVramText, 10) || 0;
    check("tenants.html displays measured GPU-seconds (4.500 s) and VRAM MiB·s (including idle lease on evict) for alpha",
      /4\.500 s/.test(alphaGpuText) && vramMibS >= 45568 && /MiB·s/.test(alphaVramText), `${alphaGpuText} | ${alphaVramText}`);

    // Update alpha quota in UI: max_vram_mb=4096, max_concurrency=12
    await page.fill(`#sl-tenant-rows tr[data-tenant="${alphaId}"] .sl-pol-vram`, "4096");
    await page.fill(`#sl-tenant-rows tr[data-tenant="${alphaId}"] .sl-pol-conc`, "12");
    await page.click(`#sl-tenant-rows tr[data-tenant="${alphaId}"] .sl-pol-save`);
    await page.waitForFunction(() => /Đã lưu hạn mức/.test(document.getElementById("sl-tenant-msg")?.textContent || ""), null, { timeout: 5000 });
    stDoc = await (await api("/api/serverless/v1/status", TOK.admin)).json();
    check("saving tenant Serverless quota updates policy on server",
      stDoc.policies?.[alphaId]?.max_vram_mb === 4096 && stDoc.policies?.[alphaId]?.max_concurrency === 12,
      JSON.stringify(stDoc.policies?.[alphaId]));

    const wideTenantsNoOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    await page.setViewportSize({ width: 375, height: 812 });
    const mobileTenantsNoOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    check("tenants.html has zero horizontal overflow at both 2560x1440 and 375x812",
      wideTenantsNoOverflow && mobileTenantsNoOverflow);

    // ---- C. Dedicated serverless.html Console (Deploy v1 -> v2 -> Rollback, Web Endpoint, Volume, Responsive) ----
    await page.setViewportSize({ width: 2560, height: 1440 });
    await page.goto(`${BASE}/serverless.html`);
    await page.waitForSelector("#sl-btn-quick-demo", { timeout: 10000 });
    await page.evaluate(() => window.AICF_READY);

    // 1-Click Quick Tenant Demo Setup
    await page.click("#sl-btn-quick-demo");
    await page.waitForFunction(() => (document.getElementById("sl-api-key")?.value || "").startsWith("aicf_"), null, { timeout: 10000 });
    const demoKey = await page.inputValue("#sl-api-key");
    check("serverless.html 1-Click Demo creates/rotates tenant key and enables Serverless", demoKey.startsWith("aicf_"), demoKey.slice(0, 10) + "…");

    // Deploy v1, then v2, then Rollback
    await page.click("#sl-btn-deploy-v1");
    await page.waitForFunction(() => document.querySelectorAll("#sl-apps-rows tr[data-app]").length >= 1, null, { timeout: 10000 });
    const ver1 = (await page.textContent("#sl-apps-rows .sl-active-ver")).trim();
    await page.click("#sl-btn-deploy-v2");
    await page.waitForFunction(v1 => {
      const cur = document.querySelector("#sl-apps-rows .sl-active-ver")?.textContent?.trim();
      return cur && cur !== v1;
    }, ver1, { timeout: 10000 });
    const ver2 = (await page.textContent("#sl-apps-rows .sl-active-ver")).trim();
    await page.click("#sl-apps-rows .sl-btn-rollback");
    await page.waitForFunction(v1 => document.querySelector("#sl-apps-rows .sl-active-ver")?.textContent?.trim() === v1, ver1, { timeout: 10000 });
    const verAfterRb = (await page.textContent("#sl-apps-rows .sl-active-ver")).trim();
    check("serverless.html deploys v1 -> v2 and rolls back to v1 cleanly",
      ver1.startsWith("v-") && ver2.startsWith("v-") && ver1 !== ver2 && verAfterRb === ver1, `${ver1} -> ${ver2} -> ${verAfterRb}`);

    // Call Web Endpoint from UI (Sync /web and Async /spawn)
    await page.click("#sl-btn-call-web");
    await page.waitForFunction(() => /e2e-ok/.test(document.getElementById("sl-web-output")?.textContent || ""), null, { timeout: 10000 });
    await page.click("#sl-btn-spawn-web");
    await page.waitForFunction(() => /Task Queue Bất đồng bộ/.test(document.getElementById("sl-web-output")?.textContent || ""), null, { timeout: 10000 });
    check("serverless.html Web Endpoint & Async Task Queue (/spawn) execute deployed function and render JSON + cURL", true);

    // Test Cloud Code Editor: custom Python source deploy & syntax check
    await page.fill("#sl-code-editor", "def classify(payload: dict) -> dict:\n    return {'custom_editor': True, 'len': len(str(payload))}\n");
    await page.click("#sl-btn-deploy-code");
    await page.waitForFunction(() => /triển khai thành công/i.test(document.getElementById("sl-deploy-msg")?.textContent || ""), null, { timeout: 10000 });
    check("serverless.html Cloud Python Code Editor compiles and deploys custom source code", true);

    // Test Cloud Logout -> locks workspace -> Register Account -> Logout -> Login with Email+Password
    await page.click("#sl-btn-logout");
    await page.waitForFunction(() => getComputedStyle(document.getElementById("sl-workspace-content")).display === "none", null, { timeout: 5000 });
    await page.fill("#sl-signup-name-input", "e2e-cloud-acct");
    await page.fill("#sl-signup-email-input", "e2e@aicf.local");
    await page.fill("#sl-signup-pw-input", "e2e-password-2026");
    await page.click("#sl-btn-register-account");
    await page.waitForFunction(() => getComputedStyle(document.getElementById("sl-workspace-content")).display !== "none", null, { timeout: 10000 });
    await page.click("#sl-btn-logout");
    await page.waitForFunction(() => getComputedStyle(document.getElementById("sl-workspace-content")).display === "none", null, { timeout: 5000 });
    await page.fill("#sl-login-id-input", "e2e@aicf.local");
    await page.fill("#sl-login-pw-input", "e2e-password-2026");
    await page.click("#sl-btn-login-account");
    await page.waitForFunction(() => getComputedStyle(document.getElementById("sl-workspace-content")).display !== "none", null, { timeout: 10000 });
    check("serverless.html Cloud Auth (Register -> Logout locks workspace -> Login with Email+Password) works end-to-end", true);

    // Test User vs Admin UI mode switcher
    const userModeHidesAdmin = await page.evaluate(() => {
      const p = document.getElementById("sl-admin-cluster-panel");
      return p && getComputedStyle(p).display === "none";
    });
    await page.click("#sl-mode-admin");
    const adminModeShowsAdmin = await page.evaluate(() => {
      const p = document.getElementById("sl-admin-cluster-panel");
      return p && getComputedStyle(p).display !== "none";
    });
    await page.click("#sl-mode-user");
    check("serverless.html separates Developer Workspace (User UI) from Admin Cluster UI",
      userModeHidesAdmin && adminModeShowsAdmin);

    // Switch to Tab 3 (Volumes) and create sample file
    await page.click("#tab-btn-volumes");
    await page.click("#sl-btn-sample-vol");
    await page.waitForFunction(() => document.querySelectorAll("#sl-vol-rows tr td.mono").length >= 1, null, { timeout: 10000 });
    const volText = await page.textContent("#sl-vol-rows");
    check("serverless.html Volume manager uploads and lists tenant volume file with SHA-256",
      /configs\/prompt_template\.json/.test(volText));

    // Switch to Tab 4 (Cloud Developer Docs) and verify Quickstart / SDK / REST API / CLI sub-tabs
    await page.click("#tab-btn-sdk");
    await page.click('.doc-tab[data-doc="doc-rest-api"]');
    const apiDocVisible = await page.evaluate(() => getComputedStyle(document.getElementById("doc-rest-api")).display !== "none");
    check("serverless.html Cloud Developer Docs hub renders Quickstart, SDK, REST API & CLI references", apiDocVisible);

    const wideSlNoOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    await page.setViewportSize({ width: 375, height: 812 });
    const mobileSlNoOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    check("serverless.html has zero horizontal overflow at both 2560x1440 and 375x812",
      wideSlNoOverflow && mobileSlNoOverflow);

    check("admin session had zero JS errors", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }

  await browser.close();
} catch (e) {
  check("serverless_e2e completed without exception", false, String(e));
} finally {
  for (const p of procs.reverse()) {
    try { p.kill("SIGTERM"); } catch {}
  }
  const pass = checks.length > 0 && checks.every(c => c.ok);
  const report = {
    schema_version: 1,
    label: "[ĐO]",
    viewports_tested: ["2560x1440", "375x812"],
    pass,
    total_checks: checks.length,
    passed_checks: checks.filter(c => c.ok).length,
    checks,
  };
  writeFileSync(OUT, JSON.stringify(report, null, 2) + "\n");
  console.log(pass ? "ALL PASS" : "FAILED");
  process.exit(pass ? 0 : 1);
}
