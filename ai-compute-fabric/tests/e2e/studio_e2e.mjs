// Real Chrome -> real site-gateway (3 roles) -> real Studio service -> real PostgreSQL, llama-servers and a real LoRA run.
//   node studio_e2e.mjs --gateway BIN --python PY --train-python PY --base DIR --embed URL --rerank URL --llm URL --dsn DSN --out report.json
import { chromium } from "playwright-core";
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";

const arg = n => process.argv[process.argv.indexOf("--" + n) + 1];
const A = Object.fromEntries(["gateway", "python", "train-python", "base", "embed", "rerank", "llm", "dsn", "out"].map(n => [n, arg(n)]));
const BASE = "http://127.0.0.1:18691", CHROME = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"].find(existsSync);
const sha = t => createHash("sha256").update(t).digest("hex");
const checks = [];
const check = (name, ok, detail = "") => { checks.push({ name, ok: !!ok, detail }); console.log((ok ? "PASS " : "FAIL ") + name + (detail ? " — " + detail : "")); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const procs = [];
const dir = mkdtempSync(join(tmpdir(), "aicf-studio-"));
const T = { viewer: "tok-v", operator: "tok-o", admin: "tok-a" };
writeFileSync(join(dir, "roles.json"), JSON.stringify([{ name: "vera", role: "viewer", token_sha256: sha(T.viewer) }, { name: "omar", role: "operator", token_sha256: sha(T.operator) }, { name: "ada", role: "admin", token_sha256: sha(T.admin) }]));
const tenant = "e2e-" + Math.random().toString(36).slice(2, 8);
const POLICY = "Chính sách nghỉ phép. Nhân viên chính thức được nghỉ phép năm 12 ngày mỗi năm. Nhân viên thử việc không được hưởng phép năm. Đơn xin nghỉ phép phải nộp trước ít nhất 3 ngày làm việc.";
const COLORS = ["đỏ", "xanh", "vàng", "tím", "cam", "trắng", "đen", "nâu"];
const DATA = Array.from({ length: 100 }, (_, i) => JSON.stringify({ question: `Mã sản phẩm AX-${String(i).padStart(3, "0")} có màu gì và bảo hành bao lâu?`, answer: `Sản phẩm AX-${String(i).padStart(3, "0")} màu ${COLORS[i % 8]}, bảo hành ${12 + 6 * (i % 4)} tháng tại mọi trung tâm của công ty.` })).join("\n");

async function open(browser, token) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 1000 }, acceptDownloads: true });
  await ctx.addInitScript(t => { try { sessionStorage.setItem("aicf_admin", t); } catch {} }, token);
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  page.on("dialog", d => d.accept());
  await page.goto(`${BASE}/studio.html`);
  await page.waitForSelector("#tabs button");
  await page.evaluate(() => window.AICF_READY);
  await page.fill("#tenant", tenant);
  await page.dispatchEvent("#tenant", "change");   // re-renders the current tab for the new tenant
  await page.waitForSelector("#panel table", { timeout: 15000 });
  await sleep(500);
  return { page, ctx, errors };
}

try {
  const env = { ...process.env, PYTHONPATH: new URL("../..", import.meta.url).pathname };
  procs.push(spawn(A.python, ["-m", "studio", "--data-dir", join(dir, "data"), "--dsn", A.dsn, "--port", "18099", "--embed-url", A.embed, "--rerank-url", A.rerank, "--llm-url", A.llm,
    "--train-python", A["train-python"], "--base-models", JSON.stringify({ "qwen3-0.6b": A.base })], { env, stdio: ["ignore", "ignore", "inherit"] }));
  procs.push(spawn(A.gateway, ["-grpc", "127.0.0.1:17945", "-http", "127.0.0.1:18691", "-roles-file", join(dir, "roles.json"), "-studio-url", "http://127.0.0.1:18099"], { stdio: "ignore" }));
  await sleep(2500);
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });

  // ---- operator does the whole job through the UI ----
  const op = await open(browser, T.operator);
  const p = op.page;
  const tabs = await p.$$eval("#tabs button", bs => bs.map(b => b.innerText));
  check("four methods are listed, full fine-tune flagged as unsupported", tabs.length === 4 && tabs.some(t => /chưa hỗ trợ/.test(t)), tabs.join(" | "));

  await p.setInputFiles("#rag-file", { name: "phep.txt", mimeType: "text/plain", buffer: Buffer.from(POLICY) });
  await p.click("button:has-text('Tải lên')");
  await p.waitForSelector("td:has-text('phep.txt')", { timeout: 20000 }).catch(async e => { throw new Error("upload did not show: msg=" + (await p.innerText("#msg")) + " can=" + (await p.evaluate(() => JSON.stringify(AICF.can))) + " files=" + (await p.evaluate(() => document.getElementById("rag-file").files.length)) + " disabled=" + (await p.locator("button:has-text('Tải lên')").isDisabled())); });
  await p.fill("#rag-q", "Nhân viên chính thức được nghỉ phép bao nhiêu ngày một năm?");
  await p.click("button:has-text('Hỏi')");
  await p.waitForSelector("#rag-answer", { timeout: 60000 });
  const ans = await p.innerText("#rag-answer"), srcs = await p.$$eval(".src", s => s.map(x => x.innerText));
  check("RAG: uploaded document is answered with its source quoted", /12/.test(ans) && srcs.length > 0 && /phep\.txt/.test(srcs[0]) && /12 ngày/.test(srcs[0]), ans.slice(0, 80));
  await p.screenshot({ path: join(dir, "studio_rag.png") });

  await p.click("#tabs button[data-id=prompts]");
  await p.waitForSelector("#p-name");
  await p.fill("#p-name", "tro-ly");
  await p.fill("#p-sys", "Trả lời ngắn gọn bằng tiếng Việt.");
  const shot = p.locator("#shots .shot").first();
  await shot.locator("textarea").nth(0).fill("Thủ đô của Pháp?");
  await shot.locator("textarea").nth(1).fill("Paris");
  await p.click("button:has-text('Lưu phiên bản mới')");
  await p.waitForSelector("td:has-text('tro-ly')", { timeout: 10000 });
  await p.fill("#p-test-q", "Thủ đô của Nhật Bản?");
  await p.click("button:has-text('So sánh trên một câu')");
  await p.waitForSelector("#p-fewshot", { timeout: 60000 });
  check("Prompts: template saved as v1 and few-shot comparison runs against the model", (await p.innerText("td:has-text('v1')").catch(() => "")).includes("v1") && (await p.innerText("#p-fewshot")).length > 0);

  await p.click("#tabs button[data-id=lora]");
  await p.waitForSelector("#l-data");
  await p.fill("#l-data", DATA);
  await p.fill("#l-steps", "60");
  await p.click("button:has-text('Kiểm tra dữ liệu')");
  await p.waitForSelector("text=Hợp lệ 100");
  await p.click("button:has-text('Bắt đầu huấn luyện')");
  await p.waitForSelector("button:has-text('Tải adapter')", { timeout: 300000 });
  const summary = await p.innerText("text=perplexity (tập giữ lại)");
  const [dl] = await Promise.all([p.waitForEvent("download"), p.click("button:has-text('Tải adapter')")]);
  const file = await dl.path();
  check("LoRA: a real job finishes, shows loss curve and perplexity before/after, adapter downloads", /→/.test(summary) && !!file && (await p.locator("#loss").count()) === 1, summary);
  console.log("LORA", summary);
  await p.screenshot({ path: join(dir, "studio_lora.png"), fullPage: true });

  await p.click("#tabs button[data-id=full_ft]");
  await p.waitForSelector("text=ESTIMATE");
  check("Full fine-tune: shown as unsupported with a reason and a labelled estimate", /Lý do chưa hỗ trợ/.test(await p.innerText("#panel")));
  check("operator: no script errors", op.errors.length === 0, op.errors.join("|"));
  check("operator cannot delete (button disabled)", await (async () => { await p.click("#tabs button[data-id=rag]"); await p.waitForSelector("td:has-text('phep.txt')"); return p.$$eval("td button", bs => bs.length > 0 && bs.every(b => b.disabled)); })());

  // ---- viewer: sees, cannot change ----
  const v = await open(browser, T.viewer);
  await v.page.waitForSelector("td:has-text('phep.txt')", { timeout: 20000 });
  check("viewer sees the read-only banner and the data, upload button disabled", (await v.page.locator("#ro").isVisible()) && await v.page.locator("button:has-text('Tải lên')").isDisabled());
  const raw = async (tok, method, path, body) => (await fetch(BASE + path, { method, headers: { Authorization: "Bearer " + tok, "Content-Type": "application/json" }, body: body && JSON.stringify(body) })).status;
  check("server agrees: viewer POST 403, operator DELETE 403, admin DELETE 200, no token 401",
    await raw(T.viewer, "POST", `/api/studio/v1/tenants/${tenant}/prompts`, { name: "x", system: "y" }) === 403
    && await raw(T.operator, "DELETE", `/api/studio/v1/tenants/${tenant}/prompts/tro-ly`) === 403
    && await raw(T.admin, "DELETE", `/api/studio/v1/tenants/${tenant}/prompts/tro-ly`) === 200
    && (await fetch(BASE + "/api/studio/v1/methods")).status === 401);
  check("viewer: no script errors", v.errors.length === 0, v.errors.join("|"));
  await browser.close();
} catch (e) {
  check("e2e ran to the end", false, String(e).slice(0, 400));
} finally {
  for (const pr of procs) pr.kill("SIGTERM");
  writeFileSync(A.out, JSON.stringify({ checks, screenshots: dir }, null, 1));
  console.log(`${checks.filter(c => c.ok).length}/${checks.length} passed; screenshots in ${dir}`);
  process.exit(checks.every(c => c.ok) ? 0 : 1);
}
