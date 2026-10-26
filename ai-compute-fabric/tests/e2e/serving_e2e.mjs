// Real Chrome -> the running demo (deploy/demo/run_demo.sh): the cluster page shows the worker's model + adapter live and switches them.
//   node serving_e2e.mjs --base http://127.0.0.1:18080 --token demo-admin-token --tenant demo --job <succeeded lora job id> --model Qwen3-0.6B-Q8_0.gguf
import { chromium } from "playwright-core";
import { existsSync } from "node:fs";

const arg = n => process.argv[process.argv.indexOf("--" + n) + 1];
const [BASE, TOKEN, TENANT, JOB, MODEL] = ["base", "token", "tenant", "job", "model"].map(arg);
const CHROME = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"].find(existsSync);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let fails = 0;
const check = (name, ok, detail = "") => { if (!ok) fails++; console.log((ok ? "PASS " : "FAIL ") + name + (detail ? " — " + detail : "")); };
const engine = async () => (await fetch("http://127.0.0.1:8099/api/engine")).json();
const waitFor = async (fn, ms = 90000) => { const end = Date.now() + ms; while (Date.now() < end) { try { if (await fn()) return true; } catch {} await sleep(300); } return false; };

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
await ctx.addInitScript(t => { try { sessionStorage.setItem("aicf_admin", t); } catch {} }, TOKEN);
const p = await ctx.newPage();
const errors = [];
p.on("pageerror", e => errors.push(String(e)));
await p.goto(`${BASE}/index.html`);
await p.evaluate(() => window.AICF_READY);
await p.waitForFunction(() => document.getElementById("sv-card").style.display !== "none" && document.querySelectorAll("#sv-model option").length > 0, null, { timeout: 20000 });
const row = () => p.locator("#rows tr").first().innerText();
check("only one management page: no separate menu entry", await p.locator("nav a[href='serving.html']").count() === 0);

// 1. switch to MODEL + adapter; watch the page (not the worker API) go through "đang đổi model" and end on the new model + adapter
await p.selectOption("#sv-model", MODEL);
await p.fill("#sv-tenant", TENANT);
await p.dispatchEvent("#sv-tenant", "change");
await p.waitForFunction(j => [...document.querySelectorAll("#sv-src option")].some(o => o.value === "job:" + j), JOB, { timeout: 10000 });
await p.selectOption("#sv-src", "job:" + JOB);
await p.click("#sv-go");
let sawSwitching = false;
const t0 = Date.now();
check("table and card end on the new model + adapter", await waitFor(async () => {
  const st = await p.textContent("#sv-state");
  if (st.includes("đang đổi")) sawSwitching = true;
  const r = await row(), now = await p.textContent("#sv-now");
  return st.trim() === "running" && r.includes(MODEL) && r.includes("demo-") && now.includes(MODEL) && now.includes("adapter demo-");
}), `${Math.round((Date.now() - t0) / 1000)} s`);
check("the page showed the intermediate state 'đang đổi model' (not a stale running)", sawSwitching);
check("worker API agrees with what the page shows", await (async () => { const e = await engine(); return e.model === MODEL && e.lora.startsWith("demo-") && e.state === "running"; })());

// 2. back to no adapter
await p.selectOption("#sv-src", "");
await p.click("#sv-go");
check("table drops the adapter again", await waitFor(async () => (await p.textContent("#sv-state")).trim() === "running" && !(await row()).includes("demo-") && (await row()).includes(MODEL)));
check("no script errors", errors.length === 0, errors.join("; "));
await browser.close();
process.exit(fails ? 1 : 0);
