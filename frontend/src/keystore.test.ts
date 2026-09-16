import { describe, expect, it, beforeEach } from "vitest";
import {
  listCommands,
  markResolved,
  newOperationKey,
  removeCommand,
  saveCommand,
} from "./keystore";
import type { StoredCommand } from "./types";

beforeEach(() => localStorage.clear());

describe("newOperationKey", () => {
  it("生成带前缀且长度足够的键", () => {
    const k = newOperationKey("create");
    expect(k.startsWith("create-")).toBe(true);
    expect(k.length).toBeGreaterThan(12);
  });

  it("两次生成互不相同", () => {
    expect(newOperationKey("accept")).not.toBe(newOperationKey("accept"));
  });
});

describe("命令持久化", () => {
  const cmd: StoredCommand = {
    kind: "create",
    key: "create-abc123abcd",
    payload: { tube_code: "T-1", operation_key: "create-abc123abcd" },
    createdAt: "2026-01-01T00:00:00Z",
  };

  it("保存后可列出（模拟关页重开）", () => {
    saveCommand(cmd);
    expect(listCommands()).toHaveLength(1);
    expect(listCommands()[0].key).toBe(cmd.key);
  });

  it("同键再次保存只保留最新一条", () => {
    saveCommand(cmd);
    saveCommand({ ...cmd, payload: { ...cmd.payload, to_staff_code: "S002" } });
    expect(listCommands()).toHaveLength(1);
  });

  it("网络失败后命令保留；标记 resolved 后从待处理列表移除但记录仍在", () => {
    saveCommand(cmd);
    expect(listCommands().filter((c) => !c.resolved)).toHaveLength(1);
    markResolved(cmd.key, 200, "HD1234");
    expect(listCommands().filter((c) => !c.resolved)).toHaveLength(0);
    const stored = listCommands()[0];
    expect(stored.resolved).toBe(true);
    expect(stored.lastStatus).toBe(200);
    expect(stored.handoffCode).toBe("HD1234");
  });

  it("可显式移除命令", () => {
    saveCommand(cmd);
    removeCommand(cmd.key);
    expect(listCommands()).toHaveLength(0);
  });

  it("localStorage 损坏时降级为空列表而非崩溃", () => {
    localStorage.setItem("handoff.pendingCommands.v1", "{not-json");
    expect(listCommands()).toEqual([]);
  });
});
