// Browser end-to-end test: real Chrome drives the gateway pages against a real
// worker-guard + llama-server. Usage:
//   node dashboard_e2e.mjs --gateway BIN --guard BIN --server BIN --model GGUF --out report.json
import { chromium } from "playwright-core";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const arg = n => process.argv[process.argv.indexOf("--" + n) + 1];
const [GW, GUARD, SERVER, MODEL, OUT] = ["gateway", "guard", "server", "model", "out"].map(arg);
const ADMIN = "e2e-admin-token", BASE = "http://127.0.0.1:18580";
const CHROME = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"].find(existsSync);
const checks = [];
const check = (name, ok, detail = "") => { checks.push({ name, ok: !!ok, detail }); console.log((ok ? "PASS " : "FAIL ") + name + (detail ? " — " + detail : "")); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const procs = [];
const dir = mkdtempSync(join(tmpdir(), "aicf-e2e-"));

async function api(path, init = {}) {
  return fetch(BASE + path, { ...init, headers: { Authorization: "Bearer " + ADMIN, ...(init.headers || {}) } });
}

try {
  procs.push(spawn(GW, ["-grpc", "127.0.0.1:17843", "-http", "127.0.0.1:18580", "-tenants-file", join(dir, "tenants.json"),
    "-admin-token", ADMIN, "-max-queue", "16"], { stdio: "ignore" }));
  await sleep(1000);
  procs.push(spawn(GUARD, ["--llama-server", SERVER, "--model", MODEL, "--ctx", "1024", "--parallel", "4", "--headroom-mib", "512",
    "--engine-port", "18590", "--listen", "127.0.0.1:18599", "--gateway", "127.0.0.1:17843", "--worker-id", "pc-001"], { stdio: "ignore" }));
  let up = false;
  for (let i = 0; i < 240 && !up; i++) {
    try { const ws = await (await api("/api/workers")).json(); up = ws[0]?.state === "running"; } catch {}
    if (!up) await sleep(500);
  }
  check("worker registered and running", up);
  if (!up) throw new Error("worker never running");
  await sleep(2500); // router cache

  const noTok = await fetch(BASE + "/api/workers");
  check("admin API refuses requests without the token", noTok.status === 401, "status " + noTok.status);

  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  // 401s are part of the flow under test (token prompt, wrong key); only real script errors count
  page.on("console", m => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
  page.on("dialog", d => d.accept(ADMIN));   // the admin-token prompt

  // 1. fleet page
  await page.goto(BASE + "/index.html");
  await page.waitForSelector("#grid .cell", { timeout: 20000 });
  const summary = await page.textContent("#sum");
  check("fleet page shows the worker on the heatmap", /1\/1 worker/.test(summary), summary);
  await page.waitForSelector("#rows tr td", { timeout: 10000 });
  check("fleet table lists pc-001 as running", /pc-001/.test(await page.textContent("#rows")) && /running/.test(await page.textContent("#rows")));
  await page.waitForSelector("#est-splits .split-box", { timeout: 10000 });
  check("VRAM estimator renders 6-bucket projection and layer split", /GREEN|YELLOW/.test(await page.textContent("#est-fit-badge")) && (await page.locator("#est-bar div").count()) === 6);
  await page.click("#grid .cell");
  await page.waitForSelector("#worker-drawer:not([hidden])", { timeout: 5000 });
  check("clicking a heatmap cell opens the 6-bucket worker drawer", /pc-001/.test(await page.textContent("#dw-title")));
  await page.click("#dw-close");

  // 2. tenants page
  await page.goto(BASE + "/tenants.html");
  await page.fill("#n", "sales"); await page.fill("#w", "2");
  await page.click("#add button.primary");
  await page.waitForSelector("#newkey:not([hidden])", { timeout: 10000 });
  const key = (await page.textContent("#keytext")).trim();
  check("creating a tenant shows its API key once", key.startsWith("aicf_"), key.slice(0, 9) + "…");
  await page.click("#dismiss");
  check("the key is gone from the page after closing", (await page.textContent("#keytext")).trim() === "");
  await page.waitForSelector("#rows tr td");
  check("tenant appears in the list with weight 2", /sales/.test(await page.textContent("#rows")) && (await page.inputValue("#rows input")) === "2");
  const list = await (await api("/api/tenants")).text();
  check("the API never returns keys or hashes", !list.includes(key) && !list.includes("key_hash"));

  // 3. chat page: valid key
  await page.goto(BASE + "/chat.html");
  await page.fill("#key", key);
  await page.fill("#q", "Chào bạn, hãy trả lời bằng một câu rất ngắn.");
  await page.click("#send");
  await page.waitForSelector(".bot .stats span", { timeout: 120000 });
  const bot = await page.textContent(".bot");
  check("chat streams an answer through the gateway", bot.length > 20, bot.slice(0, 80).replace(/\s+/g, " "));
  check("chat shows worker, time to first token and tok/s", /worker: pc-001/.test(bot) && /tok\/s/.test(bot) && /token đầu: \d+ ms/.test(bot));

  // 4. chat page: bad key and 8 parallel requests
  await page.fill("#key", "aicf_wrong");
  await page.fill("#q", "xin chào");
  await page.click("#send");
  await page.waitForFunction(() => document.querySelectorAll(".bot").length >= 2 && /401/.test(document.querySelector("#log").textContent), null, { timeout: 20000 });
  check("a wrong API key is rejected with 401 and the page says so", true);
  await page.fill("#key", key);
  await page.check("#burst");
  await page.fill("#q", "Đếm từ 1 đến 3.");
  const before = await page.locator(".bot").count();
  await page.click("#send");
  await page.waitForFunction(b => document.querySelectorAll(".bot .stats span").length >= 4 * 8 + 4 * 0 && document.querySelectorAll(".bot").length >= b + 8, before, { timeout: 180000 });
  const done = await page.locator(".bot .stats").count();
  check("8 simultaneous chats all complete", done >= 1 + 8, done + " answers with stats");

  // 5. operator pause from the fleet page
  await page.goto(BASE + "/index.html");
  await page.waitForSelector("#rows button");
  await page.click("#rows button");
  let paused = false;
  for (let i = 0; i < 20 && !paused; i++) { await sleep(500); paused = (await (await api("/api/workers")).json())[0].state === "paused"; }
  check("the pause button stops AI on that worker", paused);
  check("no JavaScript errors in any page", errors.length === 0, errors.slice(0, 3).join(" | "));
  await browser.close();
} catch (e) {
  check("test run completed without exception", false, String(e));
} finally {
  for (const p of procs.reverse()) p.kill("SIGTERM");
  await sleep(2500);
  spawnSync("pkill", ["-x", "llama-server"]);
  const pass = checks.length > 0 && checks.every(c => c.ok);
  writeFileSync(OUT, JSON.stringify({ schema_version: 1, pass, checks }, null, 2));
  console.log(pass ? "ALL PASS" : "FAILED");
  process.exit(pass ? 0 : 1);
}
