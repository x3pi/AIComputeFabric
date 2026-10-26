// Real Chrome against a real gateway with three roles: what each role SEES must match what the server lets it DO.
//   node roles_e2e.mjs --gateway BIN --loadgen BIN --out report.json
import { chromium } from "playwright-core";
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";

const arg = n => process.argv[process.argv.indexOf("--" + n) + 1];
const [GW, LG, OUT] = ["gateway", "loadgen", "out"].map(arg);
const BASE = "http://127.0.0.1:18690", CHROME = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"].find(existsSync);
const sha = t => createHash("sha256").update(t).digest("hex");
const checks = [];
const check = (name, ok, detail = "") => { checks.push({ name, ok: !!ok, detail }); console.log((ok ? "PASS " : "FAIL ") + name + (detail ? " — " + detail : "")); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const procs = [];
const dir = mkdtempSync(join(tmpdir(), "aicf-roles-"));
const roles = join(dir, "roles.json");
const T = { viewer: "tok-v", operator: "tok-o", hanoi: "tok-h", admin: "tok-a" };
writeFileSync(roles, JSON.stringify([
  { name: "vera", role: "viewer", token_sha256: sha(T.viewer) }, { name: "omar", role: "operator", token_sha256: sha(T.operator) },
  { name: "sam", role: "operator", site: "hanoi", token_sha256: sha(T.hanoi) }, { name: "ada", role: "admin", token_sha256: sha(T.admin) }]));

async function open(browser, token) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addInitScript(t => { try { sessionStorage.setItem("aicf_admin", t); } catch {} }, token);
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  page.on("dialog", d => d.dismiss());
  return { page, ctx, errors };
}

try {
  procs.push(spawn(GW, ["-grpc", "127.0.0.1:17944", "-http", "127.0.0.1:18690", "-roles-file", roles], { stdio: "ignore" }));
  await sleep(800);
  procs.push(spawn(LG, ["-grpc", "127.0.0.1:17944", "-http", "127.0.0.1:18690", "-workers", "4", "-sites", "hanoi,hcm", "-duration", "90s"], { stdio: "ignore" }));
  await sleep(3500);
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const seen = {};
  for (const [label, tok] of Object.entries(T)) {
    const { page, ctx, errors } = await open(browser, tok);
    await page.goto(`${BASE}/index.html`);
    await page.waitForSelector("#rows tr", { timeout: 15000 });
    await page.evaluate(() => window.AICF_READY);
    const badge = (await page.locator("#nav-role").innerText()).trim();
    const rowInfo = await page.$$eval("#rows tr", trs => trs.map(tr => ({ id: tr.innerText.split(/\s+/)[0], disabled: tr.querySelector("button").disabled })));
    const enabled = rowInfo.filter(r => !r.disabled).length;
    seen[label] = { badge, rows: rowInfo.length, enabled };
    await page.goto(`${BASE}/tenants.html`);
    await page.waitForSelector(".app-nav");
    await page.evaluate(() => window.AICF_READY);
    await sleep(300);
    const banner = await page.locator(".readonly-banner").count();
    const addDisabled = await page.$$eval("button", bs => bs.filter(b => /Tạo|Thêm|Lưu/.test(b.innerText)).every(b => b.disabled));
    seen[label].tenantsReadOnly = banner === 1;
    seen[label].tenantsButtonsDisabled = addDisabled;
    check(`${label}: no script errors`, errors.length === 0, errors.join("|"));
    await page.screenshot({ path: join(dir, `roles_${label}.png`) });
    await ctx.close();
  }
  console.log(JSON.stringify(seen));
  check("viewer sees a role badge and every pause button disabled", /viewer/.test(seen.viewer.badge) && seen.viewer.rows >= 4 && seen.viewer.enabled === 0, JSON.stringify(seen.viewer));
  check("operator may pause every worker", /operator/.test(seen.operator.badge) && seen.operator.enabled === seen.operator.rows, JSON.stringify(seen.operator));
  check("site-restricted operator may pause only workers of its own site", /hanoi/.test(seen.hanoi.badge) && seen.hanoi.enabled > 0 && seen.hanoi.enabled < seen.hanoi.rows, JSON.stringify(seen.hanoi));
  check("admin may pause every worker and edit tenants", /admin/.test(seen.admin.badge) && seen.admin.enabled === seen.admin.rows && !seen.admin.tenantsReadOnly, JSON.stringify(seen.admin));
  check("viewer and operator get a read-only tenants page", seen.viewer.tenantsReadOnly && seen.operator.tenantsReadOnly && seen.viewer.tenantsButtonsDisabled);

  // the UI is a convenience: the server must say the same
  const code = async (tok, method, path) => (await fetch(BASE + path, { method, headers: { Authorization: "Bearer " + tok } })).status;
  check("server: viewer pause is forbidden", await code(T.viewer, "POST", "/api/workers/sim-00000/pause") === 403);
  check("server: hanoi operator may pause a hanoi worker, not an hcm one", await code(T.hanoi, "POST", "/api/workers/sim-00000/pause") < 300 && await code(T.hanoi, "POST", "/api/workers/sim-00001/pause") === 403);
  check("server: operator may not create tenants", await code(T.operator, "POST", "/api/tenants") === 403);
  await browser.close();
} catch (e) {
  check("run completed", false, String(e));
} finally {
  for (const p of procs) p.kill("SIGTERM");
  const ok = checks.every(c => c.ok);
  if (OUT) writeFileSync(OUT, JSON.stringify({ date: new Date().toISOString(), ok, checks, screenshots: dir }, null, 1));
  console.log(ok ? "ALL PASS" : "SOME FAILED");
  process.exit(ok ? 0 : 1);
}
