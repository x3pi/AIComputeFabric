# KẾ HOẠCH: CHIA SẺ MÁY (VD. MÁY WINDOWS) CHO NGƯỜI DÙNG TRONG LAN VÀ RA INTERNET, KÈM PHÂN QUYỀN VÀ "PHIÊN" TRUY CẬP
*(Bản 2026-11-01. Chỉ là kế hoạch: chưa đổi dịch vụ đang chạy, chưa viết mã. Nhãn: `[ĐO]` đã đo · `[ĐỌC MÃ]` đã đọc trong repo · `[TÀI LIỆU]` · `[SUY LUẬN]` · `[CHƯA XÁC MINH]` phải thử.)*

---

## 0. TÓM TẮT VÀ KHUYẾN NGHỊ

1. **Chia sẻ API, không chia sẻ máy.** Người dùng (LAN hay Internet) chỉ được chạm vào *một cửa duy nhất*: `site-gateway`. Không ai được nói chuyện trực tiếp với `llama-server`, `rpc-server`, `worker-guard`, SSH, NATS hay PostgreSQL.
2. **Trước khi chia sẻ cho bất kỳ ai, phải đóng một lỗ hổng đã đo được ngay hôm nay** (mục 1.3): cổng engine `:18090` trả lời không cần khóa từ máy Windows (qua cáp trực tiếp, và rất có thể cả máy khác trên Wi-Fi), nên đi vòng qua gateway, bỏ qua mọi hạn mức.
3. **Ba mức chia sẻ, đi tuần tự, mỗi mức có cổng nghiệm thu riêng:**
   - **Mức A: LAN tin cậy** (gia đình/đồng nghiệp): khóa API theo từng người + TLS + hạn mức. Làm trước.
   - **Mức B: người dùng từ xa đã biết tên** (VPN WireGuard/Tailscale): không mở cổng công khai nào. Khuyến nghị cho "ra Internet" lần đầu.
   - **Mức C: công khai Internet**: chỉ một "tầng khách" rất hẹp, đi qua tunnel/reverse proxy, phiên ngắn hạn, hạn mức cứng. Làm cuối, và chỉ khi thật sự cần.
4. **"Phiên" truy cập** hiện **chưa tồn tại** trong gateway (khóa tenant sống vô thời hạn, không phạm vi, không hạn mức token). Đề xuất bổ sung *phiên cấp phát có hạn* (mục 4) đặt lên trên cơ chế tenant/khóa băm SHA-256 sẵn có.
5. **Máy Windows chỉ là worker nằm sau gateway** (mục 5). Nó chủ động gọi ra gateway (mTLS), không cần cổng nào mở ra Internet.

---

## 1. HIỆN TRẠNG (đã đo / đã đọc mã)

### 1.1. Cái đã có (dùng lại, không viết lại)
| Thành phần | Nội dung | Nhãn |
|---|---|---|
| Khóa API theo tenant | `tenants.go`: `id, name, key_hash (SHA-256), weight, max_inflight, disabled`; `POST/PATCH/DELETE /api/tenants`, `rotate-key` (khóa cũ chết ngay), khóa chỉ hiện một lần | `[ĐỌC MÃ]` |
| Vai trò API quản trị | `rbac.go`: `viewer` / `operator` (có thể giới hạn theo `site`) / `admin`; token băm SHA-256 từ `--roles-file`; `--admin-token` cũ vẫn là admin | `[ĐỌC MÃ]` |
| Nhật ký kiểm toán | `--audit-log`: nối chuỗi băm (hash-chained), ghi hành động quản trị | `[ĐỌC MÃ]` |
| Công bằng và giới hạn | hàng đợi công bằng theo tenant (`--fair`, `weight`), `--tenant-burst`, `--tenant-inflight`, `--max-queue`, `--max-wait`, 429 + `Retry-After` | `[ĐỌC MÃ]` |
| mTLS cho worker | gRPC `:17443` yêu cầu chứng chỉ do CA ký (`--tls-cert/--tls-key/--tls-client-ca`); `internal/pki` sinh CA + chứng chỉ gateway/worker với thời hạn tùy chọn | `[ĐỌC MÃ]` |
| Liên bang nhiều site | `apps/global-control-plane`: TLS 1.3 bắt buộc chứng chỉ client, danh sách thu hồi theo số serial, chính sách `data_class` (PUBLIC/INTERNAL/RESTRICTED), sổ đo mức dùng | `[ĐỌC MÃ]`, tác giả ghi rõ "chưa chạy qua hai site thật" |
| Giới hạn tài nguyên Studio | `studio/config.py`: số tài liệu, ký tự, prompt, job đang chạy theo tenant | `[ĐỌC MÃ]` |
| Chừa VRAM cho chủ máy | `worker-guard --headroom-mib` | `[ĐỌC MÃ]` (cờ), cơ chế chi tiết `[CHƯA XÁC MINH]` |

### 1.2. Sơ đồ mạng thực tế (đo 2026-11-01)
```
Internet ── Router 192.168.1.1 ──Wi-Fi── Linux (wlp132s0 192.168.1.40)
                                          │ ip_forward = 1
                                          │ enp133s0 10.42.0.1/24  ◄── cáp 2.5GbE trực tiếp
                                          ▼
                              Windows (Ethernet 10.42.0.225/24, đường mặc định qua 10.42.0.1)
                                          └─ WSL2 (vEthernet 172.30.64.1, cầu nccl0 10.42.0.225/32)
```
- Windows có **một** cổng vật lý (Realtek 2.5GbE, 2,5 Gbps) cắm thẳng vào Linux; **không** nằm trên router. Nó ra Internet qua Linux (`curl https://huggingface.co` từ Windows trả 200) `[ĐO]`. Địa chỉ `192.168.1.49` trong `KE_HOACH_TRIEN_KHAI_WINDOWS_192_168_1_49.md` là địa chỉ cũ, hiện không còn `[ĐO]`.
- Linux vào router bằng **Wi-Fi** (`wlp132s0`); cổng có dây `enp133s0` đang dành cho cáp tới Windows `[ĐO]`.

### 1.3. Cái đang hở (đo từ Windows qua cáp trực tiếp, 2026-11-01)
| Phép thử (chạy từ Windows `10.42.0.225` tới địa chỉ Wi-Fi của máy Linux `192.168.1.40`; gói đi qua cáp trực tiếp, không qua router) | Kết quả | Hệ quả |
|---|---|---|
| `GET :18090/health` (engine, không khóa) | **200** `[ĐO]` | Engine nghe trên mọi giao diện (`0.0.0.0`), nên máy phía cáp trực tiếp **đi vòng gateway** được: không khóa, không hạn mức, không nhật ký. Chưa thử từ thiết bị nào trên Wi-Fi `192.168.1.x`, nhưng vì cổng nghe `0.0.0.0` nên rất có thể cũng mở `[CHƯA XÁC MINH]`. Có thể đọc `/props`, gọi chat trực tiếp `[SUY LUẬN từ 200 của /props cục bộ; chưa gọi chat từ ngoài]` |
| `GET :18080/v1/models` (gateway, không khóa) | **401** `[ĐO]` | Gateway chặn đúng |
| `GET :18080/api/tenants` (không khóa) | **401** `[ĐO]` | API quản trị chặn đúng |
| Giao thức gateway `:18080` | **HTTP thuần** (`ListenAndServe`, không TLS) `[ĐỌC MÃ: main.go]` | Khóa API đi rõ trên mạng; ai nghe lén Wi-Fi LAN đều thấy. Nặng hơn khi ra Internet |
| Một cổng cho cả `/v1` lẫn `/api` quản trị | Một `mux`, một listener `[ĐỌC MÃ]` | Mở gateway ra ngoài là mở luôn bề mặt quản trị (chỉ còn token chắn) |
| Khóa tenant | Không hết hạn, không phạm vi, không hạn mức token ngày, không giới hạn IP `[ĐỌC MÃ: struct Tenant]` | Khóa lộ là lộ vĩnh viễn đến khi ai đó nhớ xoay |
| Chống dò khóa | Không thấy khóa tạm thời theo IP khi sai khóa; 429 trong mã chỉ dành cho hàng đợi `[ĐỌC MÃ, grep]` | Có thể bị dò khóa bằng vét cạn nếu lộ ra Internet |
| Kế hoạch triển khai Windows hiện có | Mở Firewall TCP `8099,18090` cho **cả** `192.168.1.0/24` `[ĐỌC MÃ: KE_HOACH_TRIEN_KHAI_WINDOWS_192_168_1_49.md, Pha 1]` | `8099` là API điều khiển worker; cần xem nó có xác thực không `[CHƯA XÁC MINH: chỉ thấy cờ `--listen`, chưa đọc handler]` |
| Dịch vụ Windows | `sc.exe create ... start= delayed-auto` không có `obj=` `[ĐỌC MÃ: install-service.ps1]` | Mặc định chạy `LocalSystem`: quyền cao nhất cục bộ. Script ghi rõ "chưa chạy trên Windows thật" |
| Tài khoản SSH Windows | `Fios02` thuộc `Administrators` `[ĐỌC MÃ: kế hoạch Windows §1.1]` | Tuyệt đối không để SSH lộ ra ngoài LAN/VPN |
| `llama.cpp rpc-server :50052` | Chạy trên giao diện cầu, máy Linux kết nối được `[ĐO: nc thành công tới 10.42.0.225:50052]` | RPC của llama.cpp không xác thực; README của llama.cpp cảnh báo là thử nghiệm và không an toàn `[TÀI LIỆU, chưa mở lại README trong phiên này]`. Chỉ được nằm trên liên kết riêng giữa hai máy |

**Việc đầu tiên không cần mã**: đóng `:18090` (và `:50052`, `:8099`) với mọi nguồn trừ gateway, rồi chạy lại đúng các phép thử trên từ máy Windows, kỳ vọng "từ chối/hết thời gian" (mục 6, Pha 0). Mình **chưa đổi** dịch vụ đang chạy.

---

## 2. MÔ HÌNH ĐE DỌA VÀ NGUYÊN TẮC

**Tài sản:** (1) thời gian GPU và điện của các máy; (2) dữ liệu của chủ (tài liệu RAG trong PostgreSQL, mô hình, mã nguồn qua Coder); (3) bản thân các máy (Windows có tài khoản Administrators); (4) khóa/chứng chỉ.

**Kẻ tấn công cần tính tới:**
- Người dùng LAN tò mò/lỡ tay (đi vòng gateway, dùng quá phần của mình).
- Thiết bị lạ trên Wi-Fi LAN (nghe lén khóa HTTP thuần).
- Khách Internet ẩn danh (dò khóa, vét hạn mức, gửi prompt độc hại, DoS nhẹ).
- Người dùng được cấp quyền nhưng làm lộ khóa (dán lên GitHub, nhật ký).
- (Chỉ mức C+/Pha 6) Chủ của một worker bên ngoài: thấy được prompt đi qua máy họ.

**Nguyên tắc:**
1. **Một cửa:** chỉ gateway lắng nghe ngoài loopback/liên kết riêng; mọi thứ còn lại bị tường lửa chặn theo nguồn, không chỉ ẩn.
2. **Mặc định từ chối:** không có khóa/phiên hợp lệ thì không có gì, kể cả đọc danh sách model.
3. **Quyền tối thiểu và giảm dần (attenuation):** phiên con không bao giờ mạnh hơn phiên/tenant cha.
4. **Ngắn hạn:** khóa dài hạn chỉ dành cho máy chủ-máy chủ; con người dùng phiên có hạn.
5. **Quan sát được:** mọi lần cấp/thu hồi phiên vào nhật ký băm chuỗi; mức dùng theo tenant có số liệu.
6. **Có công tắc ngắt:** một lệnh vô hiệu hóa tenant, thu hồi mọi phiên, hoặc dừng tunnel.
7. **Trung thực về giới hạn:** worker lạ thấy được prompt; không có mật mã nào che điều đó trong thiết kế này (mục 5.4).

---

## 3. BA MỨC CHIA SẺ

| | **A. LAN tin cậy** | **B. Từ xa, người đã biết tên** | **C. Công khai Internet** |
|---|---|---|---|
| Ai | gia đình, đồng nghiệp | vài người cụ thể, ở nhà/công ty khác | bất kỳ ai |
| Đường vào | `https://gateway.lan:18443` (TLS) | WireGuard hoặc Tailscale rồi tới gateway chỉ trên giao diện VPN | Cloudflare Tunnel hoặc Caddy/nginx phía trước gateway |
| Xác thực | phiên cấp từ khóa tenant | như A, thêm: thiết bị phải ở trong VPN (ACL theo thiết bị) | mã mời dùng một lần đổi lấy phiên ngắn hạn; tùy chọn đăng nhập OIDC |
| Mở cổng ra Internet | không | **không** (VPN đi ra, không port-forward) | không với `Tunnel`; có 443 nếu dùng reverse proxy tự lưu trữ |
| Bề mặt quản trị | listener riêng loopback/LAN quản trị | như A | **không bao giờ** lộ ra ngoài |
| Studio/Agents/RAG | theo tenant | theo tenant | tắt (chỉ chat/embeddings tầng khách) |
| Mô hình cho phép | tất cả theo tenant | theo tenant | **chỉ mô hình nhỏ**, không phải Coder-30B ghép hai máy |
| `data_class` | INTERNAL | INTERNAL | **PUBLIC** |
| Rủi ro chính | nghe lén, đi vòng gateway | thiết bị VPN bị chiếm | dò khóa, lạm dụng hạn mức, nội dung độc hại, pháp lý |
| Chi phí vận hành `[SUY LUẬN]` | thấp | thấp-vừa | cao: cần giám sát và quy tắc sử dụng |
| Khuyến nghị | **làm ngay** | **bước ra Internet đầu tiên** | **chỉ khi có nhu cầu thật** |

Lý do ưu tiên VPN trước tunnel công khai: VPN không để ai chưa có thiết bị đã đăng ký thấy được cổng nào. Mức C vẫn cần cho khách vãng lai, nhưng nên tách thành tầng riêng với hạn mức rất thấp để một sự cố không lan sang tenant thật.

---

## 4. PHÂN QUYỀN VÀ "PHIÊN" TRUY CẬP (THIẾT KẾ ĐỀ XUẤT)

### 4.1. Ai được làm gì
| Chủ thể | Tạo/xóa tenant, đặt hạn mức | Cấp phiên | Thu hồi phiên | Tạm dừng worker | Gọi suy luận |
|---|---|---|---|---|---|
| `admin` (có sẵn) | có | cho mọi tenant | mọi phiên | có | không cần |
| `operator` (có sẵn, có thể theo `site`) | không | cho tenant được giao, **không vượt hạn mức tenant** | phiên do mình cấp | trong site của mình | không cần |
| `viewer` (có sẵn) | không | không | không | không | không |
| Người dùng tenant (khóa dài) | không | **phiên con cho chính mình** (VD. cho VS Code), chỉ hẹp hơn | phiên của mình | không | theo phạm vi |
| Phiên khách | không | không | không | không | theo phạm vi hẹp |

### 4.2. Cấu trúc một phiên (mới, `[CHƯA CÓ]`)
- `id`, `tenant_id`, `issued_by` (principal), `issued_at`, `expires_at` (mặc định: khách 1 giờ; người quen 12–24 giờ; tối đa theo cấu hình).
- `scopes`: tập con của `chat`, `embeddings`, `agents`, `studio`, `bestofn`.
- `models`: danh sách cho phép; `max_tokens_per_request`, `max_context`.
- `max_inflight` ≤ của tenant; **ngân sách token** (tổng + theo ngày).
- `allowed_cidrs` (tùy chọn); `data_class` tối đa; `label` (ghi chú, VD. "anh Nam laptop").
- Bí mật: chuỗi ngẫu nhiên 256 bit, tiền tố `aicf_s_` (để quét rò rỉ), **chỉ lưu SHA-256**, hiện đúng một lần, giống `tenants.go` và `rbac.go` hiện nay.

### 4.3. Giao diện
- `POST /api/sessions` (operator trở lên): `{tenant, ttl, scopes, models, budget, cidrs}` → trả bí mật một lần.
- `DELETE /api/sessions/{id}`; vô hiệu hóa tenant thì mọi phiên của nó chết.
- `POST /v1/sessions` (khóa tenant): cấp phiên con **chỉ hẹp hơn**.
- Luồng mã mời cho Mức C: admin tạo mã mời (hiệu lực 15 phút, dùng một lần) → khách đổi mã lấy phiên tại `POST /v1/sessions/redeem` trên listener công khai, có giới hạn tốc độ theo IP. Khách không bao giờ cầm khóa dài hạn.
- Mọi cấp/thu hồi/đổi mã vào nhật ký băm chuỗi; không bao giờ ghi bí mật hoặc nội dung prompt.

### 4.4. Cách làm khớp với mã hiện có
- **Phương án A (khuyến nghị cho Pha 2):** mở rộng ngay trong Go: bảng phiên lưu tệp như `tenants.json`, kiểm tra trong cùng đường xác thực của `proxy.go`. Ít thay đổi nhất, tái dùng test và nhật ký.
- **Phương án B:** token ký Ed25519 (không trạng thái) + danh sách thu hồi. Tốt khi có nhiều gateway; thêm khóa ký cần quản lý. `[SUY LUẬN]` chưa cần cho một site.
- **Phương án C (Mức C):** đặt một proxy xác thực danh tính (Caddy/Authelia/Cloudflare Access) phía trước; gateway chỉ tin tiêu đề danh tính từ proxy qua mTLS. Thêm vào A, không thay A.

---

## 5. CHIA SẺ MÁY WINDOWS

### 5.0. Windows có cần cắm vào router không? **Không.**
Hiện Windows chỉ nối với Linux bằng cáp 2.5GbE (mục 1.2) và đó là cấu hình nên giữ:
- Người dùng khác chỉ cần tới **gateway trên Linux** (`192.168.1.40`, hoặc tên LAN, hoặc qua VPN/tunnel đặt trên Linux). Windows chỉ là worker nằm sau gateway, không ai cần địa chỉ của nó.
- Windows không nằm trên mạng của router nên thiết bị khác trên Wi-Fi không có đường trực tiếp tới `10.42.0.225` (router không có tuyến tới dải `10.42.0.0/24`) `[SUY LUẬN, chưa quét từ thiết bị Wi-Fi khác]`. Đây là lớp ngăn cách có sẵn, đừng phá nó.
- Cắm Windows vào router sẽ: mở rộng bề mặt tấn công (thêm một máy Administrators trên LAN chung), có thể làm đường ghép hai máy chậm đi nếu đi chung cổng router chỉ 1 GbE, và phải làm lại cấu hình cầu `nccl0` / luật NAT. Chỉ nên cân nhắc nếu cần dự phòng khi Linux tắt.
- **Đánh đổi của cách hiện tại:** (1) Linux là điểm lỗi duy nhất: Linux tắt thì Windows cũng không phục vụ được ai; (2) toàn bộ lưu lượng khách và cả Internet của Windows đi qua **Wi-Fi** của Linux, nên chất lượng Wi-Fi quyết định độ ổn định. Token là dữ liệu nhỏ nên băng thông hiếm khi là vấn đề, nhưng độ trễ và rớt gói thì có thể. Nên dùng cổng có dây cho Linux→router nếu có thêm card/USB-Ethernet `[CHƯA XÁC MINH]`.
- **Ra Internet:** đặt tunnel/VPN (Tailscale, Cloudflare Tunnel, WireGuard) **trên Linux**. Windows không cần cài gì thêm và không cần địa chỉ công khai.
- Việc chạy dịch vụ trên Linux có `ip_forward = 1` `[ĐO]` (để Windows ra Internet): đảm bảo tường lửa Linux **không chuyển tiếp** gói từ Wi-Fi vào `10.42.0.0/24`, nếu không thì chính Linux sẽ làm cầu cho khách tới Windows `[CHƯA XÁC MINH: chưa đọc luật FORWARD/NAT, cần quyền root]`.

### 5.1. Vai trò
Windows là **worker** sau gateway. Worker-guard gọi *ra* gateway qua gRPC mTLS (`--gateway`, `--tls-*`) `[ĐỌC MÃ]`, nên **không cần cổng nào mở vào máy Windows từ Internet**. Cổng duy nhất cần mở *vào* Windows là cổng engine, và chỉ cho địa chỉ của gateway.

### 5.2. Việc cụ thể cho máy Windows
1. Firewall: thay luật `AICF-LAN-Inbound` (cả `/24`, vốn viết cho `192.168.1.0/24`) bằng luật chỉ cho **IP gateway** (`10.42.0.1`) tới `18090`; **không mở `8099` ra LAN** (điều khiển cục bộ chỉ trên loopback hoặc có token).
2. Chạy dịch vụ bằng **tài khoản dịch vụ riêng, không phải `LocalSystem`** (`sc.exe config ... obj=`), quyền chỉ vào `C:\aicf`. `[CHƯA XÁC MINH: phải thử trên Windows thật]`.
3. SSH Administrator: chỉ khóa công khai, chỉ nghe trên LAN/VPN, không bao giờ port-forward.
4. WSL2: `rpc-server` chỉ nghe trên cầu `nccl0`/10.42.0.x và chỉ nhận từ `10.42.0.1`.
5. **Ưu tiên chủ máy:** worker-guard đã có `--headroom-mib`; thêm (chưa có) khung giờ/"nhường khi chủ đang dùng GPU" và công tắc "tạm rời cụm". `[CHƯA CÓ]`

### 5.3. Máy ở mạng khác (nhà người khác)
Gateway cần gọi tới cổng engine của worker, nên cần mạng chồng (WireGuard) giữa hai bên; nếu không, phải làm kênh ngược (worker mở kết nối ra, gateway đi trong kết nối đó) — **hiện chưa có** `[CHƯA CÓ]`. Cũng cần chứng chỉ ngắn hạn cho worker (hiện `pki.Generate` nhận `valid`, thu hồi theo serial mới có ở mặt phẳng liên bang).

### 5.4. Giới hạn trung thực khi dùng máy của người khác
Chủ worker thấy mọi prompt và có thể trả kết quả sai. Chỉ giao cho worker lạ các tác vụ `DATA_PUBLIC`, và kiểm tra chéo (VD. Best-of-N/sinh trùng) khi kết quả quan trọng. Không nên đặt mô hình/dữ liệu riêng lên máy lạ. Đây không phải điều mà mật mã trong thiết kế hiện tại giải quyết.

---

## 6. LỘ TRÌNH VÀ CỔNG NGHIỆM THU

*(Ước lượng công sức là `[SUY LUẬN]`, chưa đo.)*

| Pha | Việc | Cổng đạt | Công sức |
|---|---|---|---|
| **0. Đóng lỗ hổng, không viết mã** | (a) Engine Linux nghe `127.0.0.1` cho worker cục bộ, hoặc luật tường lửa chỉ cho nguồn gateway; (b) engine/`rpc-server` ở Windows chỉ nhận IP gateway / `10.42.0.1`; (c) bỏ `8099` khỏi luật LAN; (d) kiểm kê cổng bằng `ss -ltn` và quét từ máy khác | Từ máy Windows và một máy LAN khác: `:18090`, `:50052`, `:8099` bị từ chối; `:18080` vẫn 401 không khóa, **và** chat qua gateway với khóa vẫn chạy (kiểm hồi quy: Coder gộp vẫn trả lời) | 0,5 ngày |
| **1. TLS và tách listener** | Cờ `--http-tls-cert/--http-tls-key`; listener quản trị riêng (`--admin-listen 127.0.0.1:…`), listener công khai chỉ phục vụ `/v1/*` và đổi mã phiên; chứng chỉ gateway có SAN tên LAN | Test httptest: `/api/*` trả 404 trên listener công khai; HTTP thuần bị từ chối; khóa không còn xuất hiện trong gói tin (bắt gói kiểm) | 1 ngày |
| **2. Phiên có hạn** | Bảng phiên + `POST/DELETE /api/sessions`, `POST /v1/sessions`, đổi mã mời; hết hạn, thu hồi, ngân sách token, phạm vi, CIDR; nhật ký | Bộ test (`-race`): đúng ranh giới hết hạn; phiên con không vượt phiên cha; thu hồi có hiệu lực ngay cả với luồng đang chạy; ngân sách cạn giữa luồng cắt đúng; sống sót sau khởi động lại; chuỗi băm kiểm toán còn nguyên; bí mật không vào log. **Kiểm đột biến:** cố ý bỏ từng kiểm tra, test phải đỏ | 2–3 ngày |
| **3. Triển khai LAN (Mức A)** | Một tenant/người, `weight` và `max_inflight` hợp lý, vai trò `operator`/`viewer`, tài liệu onboarding, bảng mức dùng theo tenant | Hai người dùng cùng lúc không làm đói nhau (kiểm công bằng); khóa bị thu hồi thì mất quyền trong < 1 giây; mức dùng khớp số token đo độc lập | 1 ngày |
| **4. Từ xa qua VPN (Mức B)** | WireGuard/Tailscale; gateway nghe trên giao diện VPN; ACL theo thiết bị | Từ ngoài VPN: không cổng nào trả lời (quét từ mạng di động); trong VPN: dùng được; rút thiết bị khỏi VPN thì mất quyền | 0,5 ngày |
| **5. Công khai (Mức C)** | Tunnel/reverse proxy + giới hạn tốc độ theo IP + chống dò khóa; tầng khách (mô hình nhỏ, phiên 1 giờ, ngân sách thấp, không Studio/Agents); công tắc ngắt; cảnh báo đột biến; điều khoản sử dụng | Quét từ ngoài (nmap/ZAP) chỉ thấy 443; 1.000 lần sai khóa bị khóa tạm; vượt hạn mức thì 429 rõ ràng; công tắc ngắt có hiệu lực < 10 giây; khách không bao giờ truy cập được `/api/*` | 2 ngày + vận hành |
| **6. (Tùy chọn) Worker của người khác** | Đăng ký worker bằng CSR ký ngắn hạn, kênh ngược hoặc VPN, `DATA_PUBLIC` only, kiểm tra chéo | Chưa nên làm khi chưa có nhu cầu | tuần |

### 6.1. Đường nhanh nhất để cho một người ở nơi khác dùng thử (Mức B, khoảng 1 giờ, chưa cần viết mã)
Dữ kiện đo ngày 2026-11-01: IP công khai thấy từ Linux là `27.74.204.118`; các chặng đầu của `tracepath` không có địa chỉ `100.64.0.0/10`, nên chưa thấy dấu hiệu CGNAT, nhưng chưa so với IP WAN của router nên **chưa chắc** `[CHƯA XÁC MINH]`. IP nhà mạng nhiều khả năng là động `[SUY LUẬN]`. Trên Linux: có `wg`/`wg-quick`, **chưa** cài `tailscale` và `cloudflared` `[ĐO]`.
1. **Đóng cổng engine cục bộ (không cần root):** chạy `worker-guard` với `--engine-host 127.0.0.1 --advertise-host 127.0.0.1` (engine chỉ gateway cùng máy gọi được), rồi chạy lại `apply_coder_merged.sh`. Mất khoảng vài phút dịch vụ. Kiểm: từ Windows `curl 192.168.1.40:18090/health` phải thất bại, còn chat qua `:18080` có khóa vẫn chạy.
2. **Cài Tailscale trên Linux** (không cần mở cổng router, chịu được IP động và CGNAT). Mời đúng người đó vào tailnet; ACL chỉ cho người đó tới `tag:aicf-gw` cổng `18080`. Đường truyền đã được Tailscale mã hóa nên chưa cần TLS riêng cho bước thử này.
3. **Tạo tenant riêng cho người đó** bằng API sẵn có: `POST /api/tenants {name, weight:1, max_inflight:1}`, gửi khóa qua kênh riêng (khóa chỉ hiện một lần). Họ gọi `http://<ip-tailscale-của-linux>:18080/v1` với `Authorization: Bearer <khóa>` (giao diện kiểu OpenAI).
4. **Chặn bằng tường lửa** `18080` chỉ cho `tailscale0` và LAN (cần root; chạy `sudo`).
5. **Kết thúc thử:** `PATCH /api/tenants/{id} {"disabled": true}` hoặc `DELETE`, và xoá thiết bị khỏi tailnet.
Giới hạn của đường nhanh: khóa không hết hạn và chưa có hạn mức token/ngày (phải đợi Pha 2); chỉ nên dùng cho người quen. WireGuard thuần cũng được (`wg` đã có) nhưng cần chuyển tiếp cổng UDP trên router và tên miền động nếu IP đổi.

**Không làm (cấm):** port-forward `18090/50052/8099/22/54222(NATS)/55432(PostgreSQL)`; chia sẻ tài khoản SSH; đưa `--admin-token` vào máy khách; mở API quản trị ra listener công khai.

---

## 7. RỦI RO VÀ ĐIỀU CHƯA XÁC MINH
- Chưa gọi thử chat trực tiếp vào `:18090` từ máy khác (mới thấy `/health` = 200); khả năng bypass dựa trên 200 và việc không có `--api-key`.
- Chưa đọc handler của `worker-guard :8099` nên chưa biết nó có xác thực không.
- Chưa thử `install-service.ps1` với tài khoản riêng trên Windows thật.
- Cách tách tenant trong Studio/PostgreSQL (cô lập dữ liệu giữa tenant) chưa được kiểm thử độc lập.
- Số đo mức dùng theo tenant ở gateway chưa được kiểm chứng; `global-control-plane/metering.go` là mức site, đã ghi "chưa chạy hai site thật".
- Khuyến nghị công cụ (Tailscale/Cloudflare/Caddy) là `[SUY LUẬN]`; chưa dùng thử trong repo này. Với CGNAT/IP động của nhà mạng, tunnel gần như bắt buộc.
- Mức C kéo theo trách nhiệm nội dung và pháp lý; không thuộc phạm vi kỹ thuật của kế hoạch này.

---

## 8. QUYẾT ĐỊNH CẦN CHỦ DỰ ÁN (kèm mặc định đề xuất)
1. **Đối tượng:** chỉ LAN, thêm vài người biết tên từ xa, hay công khai? *Mặc định: A rồi B; C để sau.*
2. **Windows có dùng cá nhân (game/làm việc) không?** *Nếu có: bắt buộc quy tắc nhường chủ máy trước khi cho người khác dùng.*
3. **IP của nhà mạng:** tĩnh, động hay CGNAT? *Quyết định chọn tunnel hay port-forward.*
4. **Ghi nội dung prompt hay không?** *Mặc định: không ghi nội dung, chỉ ghi số liệu và hành động quản trị.*
5. **Hạn mức mặc định cho khách và người quen** (token/ngày, đồng thời, mô hình được dùng).
6. **Có cho phép Coder-30B ghép hai máy phục vụ người ngoài không?** *Mặc định: không; chỉ chủ và tenant tin cậy.*
