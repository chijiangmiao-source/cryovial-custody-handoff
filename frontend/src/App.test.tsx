import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// 内存版后端状态机：所有被 vi.mock 工厂引用的数据都必须放在 hoisted 块中
const m = vi.hoisted(() => {
  class MockApiError extends Error {
    constructor(
      public status: number,
      public code: string,
      message: string,
      public fields: Record<string, string> = {},
      public replay = false,
    ) {
      super(message);
    }
  }
  class MockNetworkFailure extends Error {}

  const staffList = [
    { code: "S001", name: "张敏" },
    { code: "S002", name: "李强" },
    { code: "S003", name: "王芳" },
  ];

  function makeHandoff(overrides: Record<string, unknown> = {}) {
    return {
      code: "HD000001",
      tube_code: "T-1001",
      from_staff: staffList[0],
      to_staff: staffList[1],
      status: "pending",
      custodian_staff_code: "S001",
      created_at: "2026-01-01T00:00:00+00:00",
      expires_at: "2026-01-01T00:10:00+00:00",
      accepted_at: null,
      completed_at: null,
      expired: false,
      seconds_remaining: 600,
      ...overrides,
    };
  }

  const state = { current: null as null | ReturnType<typeof makeHandoff>, createFailure: null as null | "network" | "conflict" };

  return { MockApiError, MockNetworkFailure, staffList, makeHandoff, state };
});

vi.mock("./api", () => ({
  ApiRequestError: m.MockApiError,
  NetworkFailure: m.MockNetworkFailure,
  api: {
    listStaff: vi.fn(async () => m.staffList),
    getHandoff: vi.fn(async () => m.state.current),
    getTube: vi.fn(async () => ({
      code: "T-1001",
      custodian: m.staffList[0],
      active_handoff: m.state.current,
    })),
    createHandoff: vi.fn(async () => {
      if (m.state.createFailure === "network") {
        m.state.createFailure = null;
        throw new m.MockNetworkFailure("network down");
      }
      if (m.state.createFailure === "conflict") {
        m.state.createFailure = null;
        throw new m.MockApiError(409, "active_handoff_exists", "该冻存管已有进行中的交接", {
          "/tube_code": "存在未完成的交接",
        });
      }
      m.state.current = m.makeHandoff();
      return { handoff: m.state.current, replay: false, attempts: 1 };
    }),
    acceptHandoff: vi.fn(async () => {
      m.state.current = m.makeHandoff({
        status: "accepted",
        accepted_at: "2026-01-01T00:01:00+00:00",
        seconds_remaining: 540,
      });
      return { handoff: m.state.current, replay: false, attempts: 1 };
    }),
    confirmHandoff: vi.fn(async () => {
      m.state.current = m.makeHandoff({
        status: "completed",
        accepted_at: "2026-01-01T00:01:00+00:00",
        completed_at: "2026-01-01T00:02:00+00:00",
        custodian_staff_code: "S002",
        seconds_remaining: 480,
      });
      return { handoff: m.state.current, replay: false, attempts: 1 };
    }),
  },
}));

import App from "./App";

beforeEach(() => {
  localStorage.clear();
  m.state.current = null;
  m.state.createFailure = null;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("完整交接流程", () => {
  it("创建 → 扫码接受 → 最终确认，页面显示唯一保管人 S002", async () => {
    render(<App />);

    fireEvent.click(await screen.findByTestId("create-button"));

    await waitFor(() => expect(screen.getByTestId("accept-code")).toHaveValue("HD000001"));
    await waitFor(() => expect(screen.getByTestId("handoff-status")).toHaveTextContent("待接受"));

    fireEvent.click(screen.getByTestId("accept-button"));
    await waitFor(() => expect(screen.getByTestId("handoff-status")).toHaveTextContent("已接受"));

    fireEvent.click(screen.getByTestId("confirm-button"));
    await waitFor(() => expect(screen.getByTestId("handoff-status")).toHaveTextContent("已完成"));
    expect(screen.getByTestId("custodian")).toHaveTextContent("S002");
  });
});

describe("断网与重试", () => {
  it("创建时网络失败：操作键保留并出现待处理项，重试后成功且复用同一操作键", async () => {
    render(<App />);
    await screen.findByTestId("create-button");

    m.state.createFailure = "network";
    fireEvent.click(screen.getByTestId("create-button"));

    const pending = await screen.findByTestId("pending-commands");
    expect(pending).toHaveTextContent("发起交接");
    const beforeRaw = localStorage.getItem("handoff.pendingCommands.v1")!;
    const beforeKey = JSON.parse(beforeRaw)[0].key;
    expect(beforeKey).toContain("create-");

    fireEvent.click(screen.getByTestId("retry-create"));
    await waitFor(() => expect(screen.getByTestId("accept-code")).toHaveValue("HD000001"));

    const after = JSON.parse(localStorage.getItem("handoff.pendingCommands.v1")!);
    const unresolved = after.filter((c: { resolved?: boolean }) => !c.resolved);
    expect(unresolved).toHaveLength(0);
    expect(after[0].key).toBe(beforeKey);
  });

  it("创建冲突时显示冲突提示且不产生待处理命令", async () => {
    render(<App />);
    await screen.findByTestId("create-button");

    m.state.createFailure = "conflict";
    fireEvent.click(screen.getByTestId("create-button"));

    const notice = await screen.findByTestId("notice");
    expect(notice).toHaveTextContent("已有进行中的交接");
    expect(notice.className).toContain("conflict");
    expect(screen.queryByTestId("pending-commands")).toBeNull();
  });
});

describe("关页后恢复", () => {
  it("localStorage 中的未完成命令在重新挂载后仍可用同一操作键重试完成", async () => {
    localStorage.setItem(
      "handoff.pendingCommands.v1",
      JSON.stringify([
        {
          kind: "confirm",
          key: "confirm-recovered-key-0001",
          payload: { staff_code: "S001", operation_key: "confirm-recovered-key-0001" },
          handoffCode: "HD000001",
          createdAt: "2026-01-01T00:00:00Z",
        },
      ]),
    );
    m.state.current = m.makeHandoff({ status: "accepted" });

    render(<App />);
    expect(await screen.findByTestId("retry-confirm")).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByTestId("retry-confirm"));
    });
    await waitFor(() => expect(screen.getByTestId("handoff-status")).toHaveTextContent("已完成"));
  });
});
