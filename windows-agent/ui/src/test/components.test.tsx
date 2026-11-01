import { fireEvent, render, screen } from "@testing-library/react";
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
