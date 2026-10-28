// Real Chrome E2E verification of the 3-Mode GPU Cluster Topology UI on index.html (read-only against live gateway)
// and aggregation of hardware E2E topology transition records into p3_adaptive_topology_and_large_model_e2e.json.
import { chromium } from "playwright-core";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "../..");
const REPORTS = resolve(ROOT, "benchmarks/reports");
const LOGS = resolve(REPORTS, "logs");

const BASE = process.env.AICF_BASE_URL || "http://127.0.0.1:18080";
const ADMIN = process.env.AICF_ADMIN_TOKEN || "demo-admin-token";
const OUT = process.argv.includes("--out")
  ? process.argv[process.argv.indexOf("--out") + 1]
  : resolve(REPORTS, "p3_adaptive_topology_and_large_model_e2e.json");

const CHROME = [
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find(existsSync);

const checks = [];
const check = (name, ok, detail = "") => {
  checks.push({ name, ok: !!ok, detail });
  console.log((ok ? "PASS " : "FAIL ") + name + (detail ? " — " + detail : ""));
};

function readJsonSafe(p) {
  try {
    return JSON.parse(readFileSync(p, "utf8"));
  } catch {
    return null;
  }
}

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", e => errors.push(String(e)));
page.on("console", m => {
  if (m.type() === "error" && !/Failed to load resource/.test(m.text())) {
    errors.push(m.text());
  }
});
page.on("dialog", d => d.accept(ADMIN));

try {
  await page.goto(BASE + "/index.html", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#topology-card", { timeout: 15000 });
  await page.waitForFunction(
    () => {
      const b = document.querySelector("#topo-current-badge");
      return b && b.textContent && !b.textContent.includes("ĐANG TẢI");
    },
    null,
    { timeout: 15000 }
  );

  const badgeText = (await page.textContent("#topo-current-badge")).trim();
  check(
    "topology badge displays live cluster mode",
    /ĐANG CHẠY:/.test(badgeText),
    badgeText
  );

  const summaryText = (await page.textContent("#topo-summary")).trim();
  check(
    "topology summary shows active workers or model",
    summaryText.length > 0,
    summaryText
  );

  const models = await page.$$eval("#topo-model-select option", opts =>
    opts.map(o => o.value)
  );
  check(
    "topology model selector lists 4B, 8B, 30B-A3B and Coder-30B-A3B",
    models.includes("Qwen3-4B-Q4_K_M.gguf") &&
      models.includes("Qwen3-8B-Q4_K_M.gguf") &&
      models.includes("Qwen3-30B-A3B-Q4_K_M.gguf") &&
      models.includes("Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"),
    models.join(", ")
  );

  await page.waitForSelector("#topo-nodes-container .topo-node-card", {
    timeout: 10000,
  });
  const nodeCards = await page.$$eval(
    "#topo-nodes-container .topo-node-card",
    cards => cards.map(c => c.textContent.replace(/\s+/g, " ").trim())
  );
  check(
    "topology node status cards render both pc-001 (RTX 3060) and pc-win-01 (RTX 5070)",
    nodeCards.some(t => t.includes("pc-001") && t.includes("RTX 3060")) &&
      nodeCards.some(t => t.includes("pc-win-01") && t.includes("RTX 5070")),
    nodeCards.join(" || ")
  );

  const groupsVisible = await page.$eval(
    "#topo-groups",
    el => getComputedStyle(el).display !== "none"
  );
  if (groupsVisible) {
    const groupText = (await page.textContent("#topo-groups-list")).trim();
    check(
      "merged groups board displays active group and Split button",
      groupText.includes("pc-001") &&
        groupText.includes("pc-win-01") &&
        groupText.includes("Tách nhóm"),
      groupText
    );
    const allDisabled = await page.$$eval(
      "#topo-machines input[type=checkbox]",
      cbs => cbs.length >= 2 && cbs.every(cb => cb.disabled)
    );
    check(
      "machines in an active merged group have their checkboxes disabled until split",
      allDisabled
    );
  }

  // Test mode card switching in UI (does not send POST)
  await page.click("#mode-card-smart");
  const smartVis = await page.$eval(
    "#topo-opt-smart",
    el => getComputedStyle(el).display !== "none"
  );
  check("clicking Smart 1-GPU mode card reveals -ncmoe N input panel", smartVis);

  await page.click("#mode-card-merged");
  const mergedVis = await page.$eval(
    "#topo-opt-merged",
    el => getComputedStyle(el).display !== "none"
  );
  check("clicking Merged mode card reveals ratio & KV cache panel", mergedVis);

  await page.click("#mode-card-split");
  const splitActive = await page.$eval("#mode-card-split", el =>
    el.classList.contains("active")
  );
  check("clicking Split mode card activates card A", splitActive);

  // Verify GET /v1/models on gateway with tenant key or admin check
  const topoApiResp = await fetch(BASE + "/api/cluster/topology", {
    headers: { Authorization: "Bearer " + ADMIN },
  });
  const topoJson = await topoApiResp.json();
  check(
    "GET /api/cluster/topology returns valid schema with groups and workers",
    topoApiResp.status === 200 &&
      Array.isArray(topoJson.groups) &&
      typeof topoJson.workers === "object",
    `mode=${topoJson.mode}, groups=${topoJson.groups.length}`
  );

  check(
    "zero JavaScript console or page errors on index.html",
    errors.length === 0,
    errors.slice(0, 3).join(" | ")
  );
} finally {
  await browser.close();
}

const pass = checks.length > 0 && checks.every(c => c.ok);
const report = {
  schema_version: 1,
  date: "2026-10-28",
  title:
    "P3.4b Adaptive 3-Mode GPU Cluster Topology (split, smart_1gpu, merged GGML RPC) & Large Model E2E Evidence",
  pass,
  browser_ui_e2e: {
    browser: "Google Chrome Headless (Playwright)",
    target_url: BASE + "/index.html",
    pass,
    checks,
  },
  hardware_e2e_transitions: {
    step_a_split_and_smart_pc001: readJsonSafe(resolve(LOGS, "p3_e2e_a.json")),
    step_b_merged_wifi_timeout_rollback: readJsonSafe(resolve(LOGS, "p3_e2e_b.json")),
    step_d_smart_both_30b_n26: readJsonSafe(resolve(LOGS, "p3_e2e_d.json")),
    step_f_smart_same_model_new_n_and_split: readJsonSafe(resolve(LOGS, "p3_e2e_f.json")),
    step_g_merged_4b_cable_2p5gbe: readJsonSafe(resolve(LOGS, "p3_e2e_g_merged4b.json")),
    step_h_merged_30b_cable_2p5gbe: readJsonSafe(resolve(LOGS, "p3_e2e_h_merged30b.json")),
  },
  summary_reports: [
    "benchmarks/reports/P3_BAO_CAO_CUOI_CUNG.md",
    "benchmarks/reports/P3_BANG_SO_LIEU_CUOI.md",
    "benchmarks/reports/P3_CODER_30B_GOP_2_CARD.md",
    "benchmarks/reports/P3_BAO_CAO_3_CHE_DO.md",
    "benchmarks/reports/p3_three_modes_report.json",
  ],
};

writeFileSync(OUT, JSON.stringify(report, null, 2) + "\n");
console.log(pass ? "ALL PASS -> " + OUT : "FAILED");
process.exit(pass ? 0 : 1);
