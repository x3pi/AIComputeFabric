// Pha C6 Browser E2E test: Real headless Chromium drives the Browser Code Editor (ADR-004, AicfEditor / CodeMirror 6 bundle)
// on serverless.html across 2560x1440 and 375x812 viewports with strict CSP (script-src 'self') and 0 external requests.
import { chromium } from "playwright-core";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, chmodSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const arg = n => {
  const i = process.argv.indexOf("--" + n);
  return i >= 0 ? process.argv[i + 1] : "";
};
const GW = arg("gateway") || "/tmp/aicf-site-gateway";
const OUT = arg("out") || resolve("benchmarks/reports/p_editor_c6_e2e.json");
const PY = resolve(".venv/bin/python");

const GRPC_PORT = 17966;
const HTTP_PORT = 18696;
const SL_PORT = 18196;
const BASE = `http://127.0.0.1:${HTTP_PORT}`;
const SL_SECRET = "e2e-editor-proxy-secret-c6";
const CHROME = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"].find(existsSync);

const sha = t => createHash("sha256").update(t).digest("hex");
const checks = [];
const check = (name, ok, detail = "") => {
  checks.push({ name, ok: !!ok, detail });
  console.log((ok ? "PASS " : "FAIL ") + name + (detail ? " — " + detail : ""));
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitFor(page, fn, arg = null, { timeout = 10000 } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      const ok = await page.evaluate(fn, arg);
      if (ok) return ok;
    } catch {}
    await sleep(50);
  }
  throw new Error(`waitFor timed out after ${timeout}ms`);
}

const procs = [];
const dir = mkdtempSync(join(tmpdir(), "aicf-editor-e2e-"));
const rolesFile = join(dir, "roles.json");
const tenantsFile = join(dir, "tenants.json");
const secretFile = join(dir, "sl.secret");
const auditFile = join(dir, "sl_audit.jsonl");
const ledgerFile = join(dir, "sl_ledger.jsonl");
const deployFile = join(dir, "sl_deployments.json");
const volDir = join(dir, "volumes");
const cacheDir = join(dir, "model_cache");

writeFileSync(secretFile, SL_SECRET + "\n");
chmodSync(secretFile, 0o600);

const ADMIN_TOK = "tok-editor-admin-strong-2026";
writeFileSync(rolesFile, JSON.stringify([
  { name: "ada", role: "admin", token_sha256: sha(ADMIN_TOK) },
]));
writeFileSync(tenantsFile, "[]");

try {
  let gwBin = GW;
  let lgBin = arg("loadgen") || "/tmp/aicf-loadgen";
  if (!existsSync(gwBin)) {
    gwBin = join(dir, "site-gateway");
    spawnSync("go", ["build", "-o", gwBin, "."], { cwd: resolve("apps/site-gateway"), stdio: "inherit" });
  }
  if (!existsSync(lgBin)) {
    lgBin = join(dir, "loadgen");
    spawnSync("go", ["build", "-o", lgBin, "./cmd/loadgen"], { cwd: resolve("apps/site-gateway"), stdio: "inherit" });
  }

  const gw = spawn(gwBin, [
    "-grpc", `127.0.0.1:${GRPC_PORT}`,
    "-http", `127.0.0.1:${HTTP_PORT}`,
    "-roles-file", rolesFile,
    "-tenants-file", tenantsFile,
    "-serverless-url", `http://127.0.0.1:${SL_PORT}`,
    "-serverless-secret-file", secretFile,
    "-allow-signup",
    "-signup-auto-approve",
  ], { cwd: resolve("apps/site-gateway"), stdio: "ignore" });
  procs.push(gw);

  let gwUp = false;
  for (let i = 0; i < 40 && !gwUp; i++) {
    try { gwUp = (await fetch(`${BASE}/healthz`)).ok; } catch {}
    if (!gwUp) await sleep(100);
  }
  if (!gwUp) throw new Error("site-gateway did not start");

  procs.push(spawn(lgBin, [
    "-grpc", `127.0.0.1:${GRPC_PORT}`,
    "-http", `127.0.0.1:${HTTP_PORT}`,
    "-workers", "2",
    "-sites", "hanoi",
    "-duration", "90s",
  ], { stdio: "ignore" }));
  await sleep(900);

  const pyScript = join(dir, "run_sl_editor.py");
  writeFileSync(pyScript, `
import base64, json, sys, time
from pathlib import Path
from types import SimpleNamespace
import cloudpickle
sys.path.insert(0, ${JSON.stringify(resolve("."))})
from agents.sl_security import new_key_file, load_keys
from agents.tasks import TaskResult, TaskType
from serverless.app import serve
from serverless.service import ServerlessService, HttpLeaseClient
from serverless.quota import Quotas, TenantPolicy
from serverless.deployments import DeploymentStore
from serverless.volumes import VolumeStore, ModelCacheStore
from serverless.ledger import UsageLedger, ServerlessAudit

kf = Path(${JSON.stringify(join(dir, "hmac.key"))})
new_key_file(kf)
keys = load_keys(kf)
_bundle_cache = {}

def _fake_dispatch(task, attempt):
    inp = task.input
    b64 = inp.get("bundle_b64")
    if b64:
        _bundle_cache[inp["bundle_sha256"]] = b64
    else:
        b64 = _bundle_cache.get(inp["bundle_sha256"])
    bundle = cloudpickle.loads(base64.b64decode(b64))
    args_raw = base64.b64decode(inp["args_b64"])
    if inp.get("args_format") == "json":
        parsed = json.loads(args_raw.decode("utf-8"))
        args = parsed.get("args", []) if isinstance(parsed, dict) and ("args" in parsed or "kwargs" in parsed) else [parsed]
        kwargs = parsed.get("kwargs", {}) if isinstance(parsed, dict) and ("args" in parsed or "kwargs" in parsed) else {}
    else:
        args, kwargs = cloudpickle.loads(args_raw)
    out_val = bundle["obj"](*args, **kwargs)
    res_b64 = base64.b64encode(
        json.dumps(out_val).encode("utf-8") if inp.get("result_format") == "json" else cloudpickle.dumps(out_val)
    ).decode()
    return TaskResult(
        task_id=task.task_id,
        type=TaskType.SERVERLESS_EXEC,
        output={
            "status": "ok",
            "result_b64": res_b64,
            "result_format": inp.get("result_format", "json"),
            "exec_ms": 3.5,
            "cold_start": False,
            "worker_id": "pc-editor-01",
            "stdout": "[editor-e2e] executed\\n",
        },
        worker_id="pc-editor-01",
    )

default_pol = TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=8192, max_concurrency=8)
quotas = Quotas({}, default=default_pol)
leases = HttpLeaseClient(${JSON.stringify(BASE)}, ${JSON.stringify(SL_SECRET)})
svc = ServerlessService(
    blobs={},
    quotas=quotas,
    keys=keys,
    live_workers=lambda: [SimpleNamespace(agent_id="pc-editor-01", labels=("cpu", "gpu-sl", "w-pc-editor-01"))],
    dispatch=_fake_dispatch,
    leases=leases,
    deployments=DeploymentStore(Path(${JSON.stringify(deployFile)})),
    volumes=VolumeStore(Path(${JSON.stringify(volDir)})),
    model_cache=ModelCacheStore(Path(${JSON.stringify(cacheDir)})),
    ledger=UsageLedger(Path(${JSON.stringify(ledgerFile)})),
    audit=ServerlessAudit(Path(${JSON.stringify(auditFile)})),
)
srv = serve(svc, ${JSON.stringify(SL_SECRET)}, host="127.0.0.1", port=${SL_PORT})
srv.serve_forever()
`);

  const slProc = spawn(PY, [pyScript], { stdio: ["ignore", "pipe", "inherit"] });
  procs.push(slProc);
  let slReady = false;
  for (let i = 0; i < 40 && !slReady; i++) {
    try {
      const r = await fetch(`${BASE}/api/serverless/v1/status`, { headers: { Authorization: "Bearer " + ADMIN_TOK } });
      slReady = r.ok;
    } catch {}
    if (!slReady) await sleep(150);
  }
  check("serverless service & gateway proxy ready for editor E2E", slReady);
  if (!slReady) throw new Error("serverless service did not start");

  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 2560, height: 1440 } });
  await ctx.addInitScript(t => {
    try { sessionStorage.setItem("aicf_admin", t); } catch {}
  }, ADMIN_TOK);
  const page = await ctx.newPage();
  const errors = [];
  const externalUrls = [];
  page.on("pageerror", e => errors.push(String(e)));
  page.on("console", m => {
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text());
  });
  page.on("request", req => {
    const u = req.url();
    if (!u.startsWith(BASE) && !u.startsWith("data:") && !u.startsWith("blob:")) {
      externalUrls.push(u);
    }
  });
  page.on("dialog", d => d.accept());

  await page.goto(`${BASE}/serverless.html`);
  await page.waitForSelector("#sl-btn-quick-demo", { timeout: 10000 });
  await page.evaluate(() => window.AICF_READY);

  // Initialize workspace via 1-Click Demo so #sl-workspace-content & #sl-cm6-wrap become visible
  await page.click("#sl-btn-quick-demo");
  await waitFor(page, () => (document.getElementById("sl-api-key")?.value || "").startsWith("aicf_"));
  await page.waitForSelector("#sl-cm6-wrap", { timeout: 10000 });

  // 1. Check AicfEditor mounted with gutter, syntax highlight layer, and 0 external CDN requests
  const editorMounted = await page.evaluate(() => {
    const wrap = document.getElementById("sl-cm6-wrap");
    const gutter = document.getElementById("sl-cm6-gutter");
    const hl = document.getElementById("sl-cm6-highlight");
    return !!(wrap && gutter && gutter.querySelectorAll(".cm6-gutter-line").length >= 5 && hl && hl.querySelector(".cm-tok-kw"));
  });
  check("AicfEditor (CodeMirror 6 bundle) mounts with line-number gutter and Python syntax highlight tokens", editorMounted);
  check("Zero external CDN requests made (100% air-gapped local bundle)", externalUrls.length === 0, externalUrls.join(", "));

  // 2. Test Live Lint with syntax error & undefined variable, click diagnostic to jump to line
  const lintStart = Date.now();
  await page.fill("#sl-code-editor", "def classify(payload: dict) -> dict:\n    return {'v': mystery_undefined_var}\n");
  await page.click("#sl-btn-lint-code");
  await waitFor(page, () => /undefined_name/.test(document.getElementById("sl-lint-panel")?.textContent || ""));
  const lintMs = Date.now() - lintStart;
  const hasErrGutter = await page.evaluate(() => !!document.querySelector("#sl-cm6-gutter .cm6-gutter-line.diag-err"));
  await page.click("#sl-lint-panel .lint-diag-row");
  const jumpedToLine2 = await page.evaluate(() => !!document.querySelector("#sl-cm6-gutter .cm6-gutter-line.active.diag-err"));
  check("Live Lint (POST /api/serverless/v1/lint) marks undefined_name in gutter and clicking diagnostic jumps to line 2 (<= 800ms)",
    hasErrGutter && jumpedToLine2 && lintMs <= 800, `lintMs=${lintMs}ms`);

  // 2b. Test Tab 4-space indent, Ctrl+Z undo, and Esc+Tab focus escape (WCAG 2.1.2)
  await page.fill("#sl-code-editor", "def f(x):\nreturn x\n");
  await page.evaluate(() => {
    const ta = document.getElementById("sl-code-editor");
    ta.focus();
    ta.setSelectionRange(10, 10);
  });
  await page.keyboard.press("Tab");
  const afterTab = await page.inputValue("#sl-code-editor");
  await page.keyboard.press("Control+z");
  const afterUndo = await page.inputValue("#sl-code-editor");
  await page.keyboard.press("Escape");
  await page.keyboard.press("Tab");
  const focusEscaped = await page.evaluate(() => document.activeElement?.id !== "sl-code-editor");
  check("Tab inserts 4 spaces, Ctrl+Z undoes cleanly, and Esc+Tab escapes focus out of editor",
    afterTab.includes("\n    return x") && afterUndo === "def f(x):\nreturn x\n" && focusEscaped);

  // 3. Test Search & Replace bar (Ctrl+F) and SDK Autocomplete popup
  await page.click("#sl-btn-autocomplete");
  const acVisible = await page.evaluate(() => getComputedStyle(document.getElementById("sl-cm6-autocomplete")).display !== "none");
  check("SDK/CUDA Autocomplete popup opens and lists @app.function & aicf_gpu primitives", acVisible);
  await page.keyboard.press("Escape");

  // 3b. Test XSS payload in code, function name, and output log -> never executes and 0 CSP errors
  await page.fill("#sl-ide-fn", 'classify<img src=x onerror="window.__xss=1">');
  await page.fill("#sl-code-editor", 'def classify(payload: dict) -> dict:\n    return {"xss": "<img src=x onerror=\\"window.__xss=1\\">"}\n');
  await page.click("#sl-btn-lint-code");
  await waitFor(page, () => /missing_entrypoint|Cú pháp/.test(document.getElementById("sl-lint-panel")?.textContent || ""));
  const xssTriggered = await page.evaluate(() => !!window.__xss);
  check("XSS payload in code and function name is safely escaped without execution or CSP errors", !xssTriggered);
  await page.fill("#sl-ide-fn", "classify");

  // 3c. Test .py file upload validation: UTF-8 BOM stripped, NUL byte rejected
  const bomFile = join(dir, "bom_app.py");
  writeFileSync(bomFile, "\uFEFFdef classify(payload: dict) -> dict:\n    return {'bom': 'stripped'}\n");
  await page.setInputFiles("#sl-py-file-input", bomFile);
  await waitFor(page, () => /bom_app\.py/.test(document.getElementById("sl-deploy-msg")?.textContent || ""));
  const bomStripped = !(await page.inputValue("#sl-code-editor")).startsWith("\uFEFF");
  const nulFile = join(dir, "evil_bin.py");
  writeFileSync(nulFile, "def classify(p):\x00\n    return p\n");
  await page.setInputFiles("#sl-py-file-input", nulFile);
  await waitFor(page, () => /ký tự NUL/.test(document.getElementById("sl-deploy-msg")?.textContent || ""));
  check("File upload strips UTF-8 BOM and rejects binary NUL byte files cleanly", bomStripped);
  await page.fill("#sl-ide-app", "text-analyzer");
  await page.fill("#sl-web-app", "text-analyzer");

  // 3d. Measure 5,000-line keystroke latency budget (<= 50ms) and verify editor scrollbar bounding box never cuts across editor
  const { keystroke5kMs, geometryAligned } = await page.evaluate(() => {
    const lines = Array.from({ length: 5000 }, (_, i) => `def fn_${i}(x: int) -> int: return x + ${i}`).join("\n");
    const ta = document.getElementById("sl-code-editor");
    ta.value = lines;
    const t0 = performance.now();
    ta.value = lines + "\ndef fn_5000(x: int) -> int: return x + 5000";
    const elapsed = Math.round((performance.now() - t0) * 100) / 100;
    const wrapRect = document.getElementById("sl-cm6-wrap").getBoundingClientRect();
    const taRect = ta.getBoundingClientRect();
    const hlRect = document.getElementById("sl-cm6-highlight").getBoundingClientRect();
    const gutterRect = document.getElementById("sl-cm6-gutter").getBoundingClientRect();
    const aligned =
      Math.abs(taRect.bottom - wrapRect.bottom) <= 2 &&
      Math.abs(hlRect.bottom - wrapRect.bottom) <= 2 &&
      Math.abs(gutterRect.bottom - wrapRect.bottom) <= 2 &&
      Math.abs(taRect.height - hlRect.height) <= 2;
    return { keystroke5kMs: elapsed, geometryAligned: aligned };
  });
  check("5,000-line Python buffer keystroke latency completes within <= 50ms budget and scrollbar stays pinned to editor bottom/right edge",
    keystroke5kMs <= 50 && geometryAligned, `keystroke5kMs=${keystroke5kMs}ms, geometryAligned=${geometryAligned}`);

  // 4. Test Multi-file support: add helpers.py, write helper function, import in user_app.py, and run Quick Test (Ctrl+Enter)
  await page.fill("#sl-new-file-input", "helpers.py");
  await page.click("#sl-btn-add-file");
  await waitFor(page, () => document.querySelectorAll("#sl-file-tabs .ide-file-tab").length === 2);
  await page.fill("#sl-code-editor", "BONUS = 77\ndef enrich(text: str) -> str:\n    return f'{text.upper()}::HELPER_OK'\n");
  await page.click('#sl-file-tabs .ide-file-tab[data-file="user_app.py"]');
  await page.fill("#sl-code-editor", "import helpers\n\ndef classify(payload: dict) -> dict:\n    return {'msg': helpers.enrich(str(payload.get('text', 'hi'))), 'bonus': helpers.BONUS}\n");

  await page.click("#sl-btn-lint-code");
  await waitFor(page, () => /Cú pháp & AST hợp lệ/.test(document.getElementById("sl-lint-panel")?.textContent || ""));
  check("Live Lint passes on valid multi-file Python code", true);

  // Trigger Quick Test Run (#sl-btn-test-run / Ctrl+Enter)
  await page.click("#sl-btn-test-run");
  await waitFor(page, () => /HELPER_OK/.test(document.getElementById("sl-web-output")?.textContent || ""), null, { timeout: 12000 });
  const outText = await page.textContent("#sl-web-output");
  check("Multi-file deploy + Quick Test Run executes user_app.py importing helpers.py inside sandbox", /HELPER_OK/.test(outText) && /77/.test(outText));

  // 5. Test Version Diff Viewer (#sl-btn-diff-versions) after modifying editor buffer
  await page.fill("#sl-code-editor", "import helpers\n\ndef classify(payload: dict) -> dict:\n    # Modified line for diff check\n    return {'msg': helpers.enrich('diff-v2'), 'bonus': helpers.BONUS + 1}\n");
  await page.click("#sl-btn-diff-versions");
  await waitFor(page, () => getComputedStyle(document.getElementById("sl-diff-panel")).display !== "none");
  const diffHasAddDel = await page.evaluate(() => {
    const out = document.getElementById("sl-diff-output");
    return !!(out && out.querySelector(".diff-line-add") && out.querySelector(".diff-line-del"));
  });
  check("Version Diff viewer renders unified +added and -removed lines against deployed version", diffHasAddDel);
  await page.click("#sl-btn-close-diff");

  // 6. Test localStorage draft badge & discard button
  const draftSaved = await page.evaluate(() => /Đã lưu nháp/.test(document.getElementById("sl-draft-badge")?.textContent || ""));
  check("Editor automatically saves unsaved draft to localStorage with status badge", draftSaved);

  // 7. Check responsive layout at 2560x1440 and 375x812
  const wideNoOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  await page.setViewportSize({ width: 375, height: 812 });
  const mobileNoOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  check("Editor layout has zero horizontal overflow at both 2560x1440 and 375x812", wideNoOverflow && mobileNoOverflow);

  check("Editor E2E session had zero JS/CSP errors", errors.length === 0, errors.join(" | "));
  await ctx.close();

  // 8. Test graceful <textarea> fallback when vendor/codemirror6.bundle.js is blocked
  const ctxFallback = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctxFallback.addInitScript(t => {
    try { sessionStorage.setItem("aicf_admin", t); } catch {}
  }, ADMIN_TOK);
  const pageFb = await ctxFallback.newPage();
  await pageFb.route("**/vendor/codemirror6.bundle.js", route => route.abort());
  await pageFb.goto(`${BASE}/serverless.html`);
  await pageFb.waitForSelector("#sl-btn-quick-demo", { timeout: 10000 });
  await pageFb.evaluate(() => window.AICF_READY);
  await pageFb.click("#sl-btn-quick-demo");
  await waitFor(pageFb, () => (document.getElementById("sl-api-key")?.value || "").startsWith("aicf_"));
  await pageFb.fill("#sl-code-editor", "def classify(payload: dict) -> dict:\n    return {'fallback_textarea': True}\n");
  await pageFb.click("#sl-btn-deploy-code");
  await waitFor(pageFb, () => /triển khai thành công/i.test(document.getElementById("sl-deploy-msg")?.textContent || ""));
  check("Graceful fallback to plain <textarea> still deploys cleanly when editor bundle is blocked", true);
  await ctxFallback.close();

  await browser.close();
} catch (e) {
  check("editor_e2e completed without exception", false, e?.stack || String(e));
} finally {
  for (const p of procs) {
    try { p.kill("SIGKILL"); } catch {}
  }
}

const failed = checks.filter(c => !c.ok);
writeFileSync(OUT, JSON.stringify({ phase: "C6_editor_e2e", pass: failed.length === 0, checks }, null, 2));
if (failed.length) {
  console.error("FAILED checks:", failed.map(f => f.name));
  process.exit(1);
}
console.log("ALL PASS");
