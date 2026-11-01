import { describe, expect, it } from "vitest";
import { parseStepLine, RunRequest, State, toolRisk } from "../lib/schema";

describe("parseStepLine", () => {
  it("parses a normal step", () => {
    const s = parseStepLine('[3] {"tool": "set_text", "args": {"id": 1, "text": "hi"}} -> da dat van ban');
    expect(s).toEqual({ n: 3, tool: "set_text", args: { id: 1, text: "hi" }, result: "da dat van ban" });
  });
  it("keeps multi-line results", () => {
    expect(parseStepLine('[1] {"tool": "observe", "args": {}} -> a | b')?.result).toBe("a | b");
  });
  it("rejects garbage and missing tool", () => {
    expect(parseStepLine("hello")).toBeNull();
    expect(parseStepLine('[1] {"args": {}} -> x')).toBeNull();
    expect(parseStepLine("[1] {not json} -> x")).toBeNull();
  });
});

describe("toolRisk", () => {
  it("classifies", () => {
    expect(toolRisk("observe")).toBe("low");
    expect(toolRisk("write_file")).toBe("medium");
    expect(toolRisk("run_shell")).toBe("high");
  });
});

describe("RunRequest", () => {
  it("validates url and task", () => {
    expect(RunRequest.safeParse({ url: "nope", task: "abc" }).success).toBe(false);
    expect(RunRequest.safeParse({ url: "http://a/v1", task: "  " }).success).toBe(false);
    expect(RunRequest.safeParse({ url: "http://a/v1", task: "lam gi do" }).success).toBe(true);
  });
});

describe("State", () => {
  it("rejects unknown status from server", () => {
    expect(State.safeParse({ status: "weird", log: [], pending: [] }).success).toBe(false);
    expect(State.safeParse({ status: "running", log: ["x"], pending: [] }).success).toBe(true);
  });
});
