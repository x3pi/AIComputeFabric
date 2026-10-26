// Real Chrome -> the running demo (deploy/demo/run_demo.sh): the "Model đang chạy" page switches the worker's model and adapter.
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
const waitFor = async (fn, ms = 90000) => { const end = Date.now() + ms; while (Date.now() < end) { try { if (await fn()) return true; } catch {} await sleep(1000); } return false; };

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
async function page(token) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addInitScript(t => { try { sessionStorage.setItem("aicf_admin", t); } catch {} }, token);
  const p = await ctx.newPage();
  p.errors = [];
  p.on("pageerror", e => p.errors.push(String(e)));
  await p.goto(`${BASE}/serving.html`);
  await p.evaluate(() => window.AICF_READY);
  await p.waitForFunction(() => document.querySelectorAll("#model option").length > 0, null, { timeout: 15000 });
  return p;
}

const p = await page(TOKEN);
check("page lists models and shows the current one", (await p.textContent("#now")).includes((await engine()).model));
check("navigation links to the page", await p.locator("nav a[href='serving.html']").count() === 1);
await p.selectOption("#model", MODEL);
await p.fill("#tenant", TENANT);
await p.dispatchEvent("#tenant", "change");
await p.waitForFunction(j => [...document.querySelectorAll("#src option")].some(o => o.value === "job:" + j), JOB, { timeout: 10000 });
await p.selectOption("#src", "job:" + JOB);
await p.click("#go");
check("worker ends up serving the chosen model + exported adapter", await waitFor(async () => { const e = await engine(); return e.state === "running" && e.model === MODEL && e.lora.endsWith(".gguf") && e.lora !== ""; }), JSON.stringify(await engine()).slice(0, 160));
await p.waitForFunction(m => document.querySelector("#now").textContent.includes(m) && document.querySelector("#now").textContent.includes("adapter"), MODEL, { timeout: 15000 });
check("page shows model + adapter after the switch", true, await p.textContent("#now"));
await p.selectOption("#src", "");
await p.click("#go");
check("worker switches back to no adapter", await waitFor(async () => { const e = await engine(); return e.state === "running" && e.model === MODEL && e.lora === ""; }));
check("no script errors", p.errors.length === 0, p.errors.join("; "));
await browser.close();
process.exit(fails ? 1 : 0);
