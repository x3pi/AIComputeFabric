# ADR-001 — GPUStack: tái dùng thư viện, không dùng Worker trên PC Windows

- **Trạng thái:** PROPOSED (chờ cổng P0 xác nhận lại bằng số đo thật)
- **Ngày:** 2026-10-25
- **Quyết định (D01/D02):** tự xây `worker-guard` (Go) quản vòng đời `llama-server`; dùng `gguf-parser-go` (MIT) như thư viện; học UX của GPUStack; GPUStack Server/Worker không vào production worker.

## Bằng chứng
- Trang repo `gpustack/gpustack` (tóm tắt tự động, 2026-10-25): Apache-2.0; "Only Linux is supported for GPUStack worker nodes" (Windows qua WSL2); backend nêu: vLLM, SGLang, TensorRT-LLM; không nhắc llama.cpp.
- Fleet mục tiêu là PC Windows của nhân viên với ưu tiên người dùng ⇒ không khớp mô hình worker.

## Chưa được kiểm chứng (việc của P0, ngày 1–2)
- Đọc lại tài liệu chính thức (trang tóm tắt có thể sai/lỗi thời).
- Hỗ trợ llama.cpp / Windows ở phiên bản GPUStack hiện hành.
- Khả năng chịu 200 worker, ưu tiên người dùng không cần fork.

## Điều kiện đảo quyết định
Fleet ≥ 80 % Linux **và** GPUStack đạt điều kiện W2/W4 (xem kế hoạch §B.0b) ⇒ viết ADR bản 2.
