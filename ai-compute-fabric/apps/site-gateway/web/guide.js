const origin = location.origin || "http://127.0.0.1:18080";
document.getElementById("code-curl").textContent =
`curl -N "${origin}/v1/chat/completions" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer aicf_YOUR_API_KEY" \\
  -H "X-Session-ID: conv-demo-01" \\
  -d '{
    "stream": true,
    "max_tokens": 256,
    "messages": [
      {"role": "user", "content": "Giải thích cơ chế 6-Bucket VRAM của AI Compute Fabric."}
    ]
  }'`;

document.getElementById("code-py").textContent =
`from openai import OpenAI

client = OpenAI(
    base_url="${origin}/v1",
    api_key="aicf_YOUR_API_KEY",  # Khóa lấy từ trang Người dùng & Khóa API
)

stream = client.chat.completions.create(
    model="Qwen3-4B",
    messages=[{"role": "user", "content": "Xin chào! Hãy tóm tắt 3 ưu điểm của hệ thống."}],
    extra_headers={"X-Session-ID": "session-user-42", "X-Priority": "interactive"},
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)`;

renderSixBuckets(document.getElementById("doc-demo-bar"), null, {
  Total: 12288 * 1048576,
  OSUser: 650 * 1048576,
  Weights: 4950 * 1048576,
  KVState: 1850 * 1048576,
  Compute: 380 * 1048576,
  Headroom: 1536 * 1048576,
  Allocatable: 2922 * 1048576,
});

async function copyCode(id, btn) {
  const txt = document.getElementById(id).textContent;
  try {
    await navigator.clipboard.writeText(txt);
    const old = btn.textContent;
    btn.textContent = "Đã sao chép ✓";
    setTimeout(() => { btn.textContent = old; }, 1800);
  } catch {}
}
const linkSecApi = document.getElementById("link-sec-api");
if (linkSecApi) {
  linkSecApi.addEventListener("click", e => {
    e.preventDefault();
    const sec = document.getElementById("sec-api");
    if (sec) sec.scrollIntoView({ behavior: "smooth" });
  });
}
document.querySelectorAll("[data-copy-target]").forEach(btn => {
  btn.addEventListener("click", () => copyCode(btn.getAttribute("data-copy-target"), btn));
});
