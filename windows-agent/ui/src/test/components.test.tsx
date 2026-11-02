import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApprovalCards } from "../components/ApprovalCard";
import { ConfigForm } from "../components/ConfigForm";
import { StepTimeline } from "../components/StepTimeline";

describe("ConfigForm", () => {
  it("blocks submit and shows errors when task is empty", () => {
    const onRun = vi.fn();
    render(<ConfigForm disabled={false} onRun={onRun} />);
    fireEvent.click(screen.getByRole("button", { name: "Chạy" }));
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.getByText(/ít nhất 3 ký tự/)).toBeTruthy();
  });
  it("submits valid input", () => {
    const onRun = vi.fn();
    render(<ConfigForm disabled={false} onRun={onRun} />);
    fireEvent.change(screen.getByLabelText("Nhiệm vụ"), { target: { value: "mo notepad" } });
    fireEvent.click(screen.getByRole("button", { name: "Chạy" }));
    expect(onRun).toHaveBeenCalledOnce();
    expect(onRun.mock.calls[0]?.[0].task).toBe("mo notepad");
  });
  it("is disabled while running", () => {
    render(<ConfigForm disabled onRun={() => {}} />);
    expect((screen.getByRole("button", { name: "Đang chạy…" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("ConfigForm - chọn model", () => {
  it("loads models into a select and submits the chosen one", async () => {
    const onRun = vi.fn();
    const load = vi.fn().mockResolvedValue(["qwen-a", "qwen-b"]);
    render(<ConfigForm disabled={false} onRun={onRun} loadModels={load} />);
    fireEvent.change(screen.getByLabelText(/API key/), { target: { value: "k1" } });
    fireEvent.click(screen.getByRole("button", { name: "Tải danh sách" }));
    await waitFor(() => expect((screen.getByLabelText(/^Model/) as HTMLElement).tagName).toBe("SELECT"));
    const sel = screen.getByLabelText(/^Model/) as HTMLSelectElement;
    expect(load).toHaveBeenCalledWith("http://10.42.0.1:18090/v1", "k1");
    fireEvent.change(sel, { target: { value: "qwen-b" } });
    fireEvent.change(screen.getByLabelText("Nhiệm vụ"), { target: { value: "mo notepad" } });
    fireEvent.click(screen.getByRole("button", { name: "Chạy" }));
    expect(onRun.mock.calls[0]?.[0].model).toBe("qwen-b");
  });
  it("shows the error and keeps a free-text input when loading fails", async () => {
    const load = vi.fn().mockRejectedValue(new Error("server model tra HTTP 401 (can API key?)"));
    render(<ConfigForm disabled={false} onRun={() => {}} loadModels={load} />);
    fireEvent.click(screen.getByRole("button", { name: "Tải danh sách" }));
    expect(await screen.findByText(/API key\?/)).toBeTruthy();
    expect((screen.getByLabelText(/^Model/) as HTMLElement).tagName).toBe("INPUT");
  });
});

describe("ApprovalCards", () => {
  const item = { id: "a1", reason: "ghi de file", action: { tool: "write_file", args: { path: "a.txt" } } };
  it("calls onDecide with allow/deny", () => {
    const f = vi.fn();
    render(<ApprovalCards items={[item]} onDecide={f} />);
    fireEvent.click(screen.getByText("Cho phép"));
    fireEvent.click(screen.getByText("Từ chối"));
    expect(f).toHaveBeenNthCalledWith(1, "a1", true);
    expect(f).toHaveBeenNthCalledWith(2, "a1", false);
  });
  it("renders nothing when empty", () => {
    const { container } = render(<ApprovalCards items={[]} onDecide={() => {}} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("StepTimeline", () => {
  it("shows empty state and steps", () => {
    const { rerender } = render(<StepTimeline log={[]} />);
    expect(screen.getByText(/Chưa có bước nào/)).toBeTruthy();
    rerender(<StepTimeline log={['[1] {"tool": "read_file", "args": {"path": "a"}} -> TU CHOI: x']} />);
    expect(screen.getByText("1. read_file")).toBeTruthy();
    expect(screen.getByText("TU CHOI: x").className).toContain("text-bad");
  });
});
