// Browser check of screen 4a against a real gateway, the real dag_api and a real half-finished workflow.
//   node agents_dag_e2e.mjs --gateway BIN --python PY --dsn DSN --out report.json
import { chromium } from "playwright-core";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const arg = n => process.argv[process.argv.indexOf("--" + n) + 1];
const [GW, PY, DSN, OUT] = ["gateway", "python", "dsn", "out"].map(arg);
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const ADMIN = "dag-admin-token", BASE = "http://127.0.0.1:18680", WF = "e2e-" + Date.now();
const CHROME = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"].find(existsSync);
const checks = [];
const check = (name, ok, detail = "") => { checks.push({ name, ok: !!ok, detail }); console.log((ok ? "PASS " : "FAIL ") + name + (detail ? " — " + detail : "")); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const procs = [];
const dir = mkdtempSync(join(tmpdir(), "aicf-dag-"));
const hold = join(dir, "hold");
writeFileSync(hold, "x");
const env = { ...process.env, PYTHONPATH: ROOT };

try {
  procs.push(spawn(PY, ["-m", "agents.dag_api", "--dsn", DSN, "--listen", "127.0.0.1:18681"], { cwd: ROOT, env, stdio: "ignore" }));
  procs.push(spawn(GW, ["-grpc", "127.0.0.1:17943", "-http", "127.0.0.1:18680", "-admin-token", ADMIN, "-agents-url", "http://127.0.0.1:18681"], { stdio: "ignore" }));
  procs.push(spawn(PY, ["tests/e2e/seed_agent_workflow.py", DSN, WF, hold], { cwd: ROOT, env, stdio: "inherit" }));
  const auth = { headers: { Authorization: "Bearer " + ADMIN } };
  let snap = null;
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(`${BASE}/api/agents/workflows/${WF}`, auth); if (r.ok) { snap = await r.json(); if (snap.counts.running) break; } } catch {}
    await sleep(250);
  }
  check("gateway proxies the workflow, with one step running", snap && snap.counts.running === 1, JSON.stringify(snap && snap.counts));
  check("no credentials, no data", (await fetch(`${BASE}/api/agents/workflows`)).status === 401);
  const raw = JSON.stringify(snap);
  check("response carries no task text", !raw.includes("private"));

  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  page.on("console", m => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
  page.on("dialog", d => d.accept(ADMIN));
  await page.goto(`${BASE}/agents.html?wf=${WF}`);
  await page.waitForSelector("svg#dag g.node", { timeout: 15000 });
  const n = await page.locator("svg#dag g.node").count();
  check("one box per step (14)", n === 14, "found " + n);
  check("edges drawn (dependencies)", (await page.locator("svg#dag path.edge").count()) === 4 * 2 + 4 + 1, "found " + await page.locator("svg#dag path.edge").count());
  check("the held step is drawn as running", (await page.locator("svg#dag g.node.s-running").count()) === 1);
  check("finished steps are drawn as ok", (await page.locator("svg#dag g.node.s-ok").count()) >= 12);
  check("the step after the held one is pending", (await page.locator("svg#dag g.node.s-pending").count()) === 1);
  await page.locator("svg#dag g.node.s-running").click();
  const detail = await page.locator("#detail").innerText();
  check("clicking a box shows its details", /merge\.hold/.test(detail) && /đang chạy/.test(detail) && /doc0\.summarise/.test(detail), detail);
  check("page text carries no task content", !(await page.locator("body").innerText()).includes("private"));
  await page.screenshot({ path: join(dir, "dag_running.png") });

  unlinkSync(hold);   // release the step; the page polls every 2.5 s
  let done = false;
  for (let i = 0; i < 30 && !done; i++) { await sleep(500); done = (await page.locator("svg#dag g.node.s-ok").count()) === 14; }
  check("after release every box turns ok and the workflow reads done", done && /hoàn tất/.test(await page.locator("#wf-state").innerText()));
  await page.screenshot({ path: join(dir, "dag_done.png") });
  check("no script errors", errors.length === 0, errors.join(" | "));
  await browser.close();
} catch (e) {
  check("run completed", false, String(e));
} finally {
  for (const p of procs) p.kill("SIGTERM");
  spawnSync(PY, ["-c", "import sys,psycopg;c=psycopg.connect(sys.argv[1],autocommit=True);[c.execute(f'DELETE FROM {t} WHERE workflow=%s',(sys.argv[2],)) for t in ('aicf_dag','aicf_results','aicf_exec')]", DSN, WF]);
  writeFileSync(OUT, JSON.stringify({ date: new Date().toISOString(), pass: checks.every(c => c.ok), checks, screenshots: dir }, null, 1));
  console.log("screenshots in", dir);
  process.exit(checks.every(c => c.ok) ? 0 : 1);
}
