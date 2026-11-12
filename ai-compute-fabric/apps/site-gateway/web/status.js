(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  function ago(sec) {
    if (sec < 60) return sec + " giây trước";
    if (sec < 3600) return Math.floor(sec / 60) + " phút trước";
    return Math.floor(sec / 3600) + " giờ trước";
  }
  function render(u) {
    var st = $("state"), min = Math.round(u.window_seconds / 60);
    var cls, emoji, label, sub;
    if (u.slots === 0) {
      cls = "down"; emoji = "⚪"; label = "Không có máy phục vụ"; sub = "Chưa có máy chạy model nào sẵn sàng.";
    } else if (u.queued > 0) {
      cls = "full"; emoji = "🔴"; label = "Đang đông, có hàng đợi";
      sub = u.queued + " yêu cầu đang chờ tới lượt.";
    } else if (u.inflight >= u.slots) {
      cls = "full"; emoji = "🟠"; label = "Đã kín chỗ";
      sub = "Mọi luồng đều bận, yêu cầu mới có thể phải chờ.";
    } else if (u.inflight > 0) {
      cls = "busy"; emoji = "🟡"; label = "Đang có người dùng";
      sub = "Còn " + (u.slots - u.inflight) + " luồng trống.";
    } else {
      cls = "idle"; emoji = "🟢"; label = "Đang rảnh";
      sub = u.last_request_unix ? "Yêu cầu gần nhất: " + ago(Math.max(0, u.now_unix - u.last_request_unix)) : "Chưa có yêu cầu nào kể từ lúc khởi động.";
    }
    st.className = "state " + cls;
    $("emoji").textContent = emoji; $("label").textContent = label; $("sub").textContent = sub;
    var sl = $("slots"); sl.textContent = "";
    for (var i = 0; i < u.slots; i++) {
      var d = document.createElement("div");
      d.className = "slot" + (i < u.inflight ? " on" : "");
      sl.appendChild(d);
    }
    $("busy").textContent = u.users_busy; $("queued").textContent = u.queued;
    $("recent").textContent = u.users_recent; $("reqs").textContent = u.requests_recent;
    $("recentT").textContent = "người dùng trong " + min + " phút qua";
    $("reqsT").textContent = "yêu cầu trong " + min + " phút qua";
    $("foot").textContent = "Cập nhật " + new Date().toLocaleTimeString("vi-VN") + " · tự làm mới mỗi 3 giây · " + u.inflight + "/" + u.slots + " luồng đang bận";
  }
  function fail() {
    $("state").className = "state down"; $("emoji").textContent = "⚠️";
    $("label").textContent = "Không đọc được trạng thái"; $("sub").textContent = "Gateway không phản hồi.";
  }
  function tick() {
    var c = new AbortController(), t = setTimeout(function () { c.abort(); }, 4000);
    fetch("/api/usage", { cache: "no-store", signal: c.signal })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(render, fail)
      .then(function () { clearTimeout(t); });
  }
  tick(); setInterval(tick, 3000);
})();
