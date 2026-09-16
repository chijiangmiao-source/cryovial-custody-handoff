import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { TubeHistory } from "../types";

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

  const tube = {
    code: "T-1001",
    custodian: { code: "S002", name: "李强" },
    active_handoff: null,
  };

  function history(overrides: Partial<TubeHistory> = {}): TubeHistory {
    return {
      tube_code: "T-1001",
      queried_at: "2026-09-16T23:00:00+08:00",
      point_in_time: "2026-09-16T20:00:00+08:00",
      traceable_since: "2026-09-16T18:00:00+08:00",
      evidence_available: true,
      custodian: { code: "S001", name: "张敏" },
      event_seq: 0,
      handoff_code: null,
      previous_change: null,
      next_change: {
        seq: 1,
        kind: "transfer",
        effective_at: "2026-09-16T21:00:00+08:00",
        custodian: { code: "S002", name: "李强" },
        from_staff: { code: "S001", name: "张敏" },
        to_staff: { code: "S002", name: "李强" },
        handoff_code: "AB12CD34",
        active_at_point: false,
      },
      timeline: [
        {
          seq: 0,
          kind: "baseline",
          effective_at: "2026-09-16T18:00:00+08:00",
          custodian: { code: "S001", name: "张敏" },
          from_staff: null,
          to_staff: null,
          handoff_code: null,
          active_at_point: true,
        },
        {
          seq: 1,
          kind: "transfer",
          effective_at: "2026-09-16T21:00:00+08:00",
          custodian: { code: "S002", name: "李强" },
          from_staff: { code: "S001", name: "张敏" },
          to_staff: { code: "S002", name: "李强" },
          handoff_code: "AB12CD34",
          active_at_point: false,
        },
      ],
      ...overrides,
    };
  }

  const state = {
    historyFailure: null as null | "broken" | "missing" | "network",
    lastAt: null as Date | null,
  };

  return { MockApiError, MockNetworkFailure, tube, history, state };
});

vi.mock("../api", () => ({
  ApiRequestError: m.MockApiError,
  NetworkFailure: m.MockNetworkFailure,
  api: {
    getTube: vi.fn(async () => m.tube),
    getTubeHistory: vi.fn(async (_code: string, at: Date | null) => {
      m.state.lastAt = at;
      if (m.state.historyFailure === "broken") {
        m.state.historyFailure = null;
        throw new m.MockApiError(409, "custody_chain_broken", "保管账本断链");
      }
      if (m.state.historyFailure === "missing") {
        m.state.historyFailure = null;
        throw new m.MockApiError(409, "custody_baseline_missing", "缺少基线");
      }
      if (m.state.historyFailure === "network") {
        m.state.historyFailure = null;
        throw new m.MockNetworkFailure("offline");
      }
      return m.history();
    }),
  },
}));

import { LookupPanel } from "./LookupPanel";

beforeEach(() => {
  m.state.historyFailure = null;
  m.state.lastAt = null;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function lookupTube() {
  fireEvent.change(screen.getByTestId("lookup-input"), { target: { value: "T-1001" } });
  fireEvent.click(screen.getByTestId("lookup-button"));
  await screen.findByTestId("lookup-custodian");
}

describe("历史时间点查询", () => {
  it("返回保管人、序号、交接码、前后变更项并渲染时间轴", async () => {
    render(<LookupPanel onSelectHandoff={vi.fn()} />);
    await lookupTube();

    fireEvent.change(screen.getByTestId("history-time-input"), {
      target: { value: "2026-09-16T20:00:00" },
    });
    fireEvent.click(screen.getByTestId("history-button"));

    await screen.findByTestId("history-result");
    expect(screen.getByTestId("history-custodian")).toHaveTextContent("S001");
    expect(screen.getByTestId("history-seq")).toHaveTextContent("0");
    expect(screen.getByTestId("history-event-0")).toHaveAttribute("data-active", "true");
    expect(screen.getByTestId("history-event-1")).toHaveAttribute("data-active", "false");
    expect(screen.getByTestId("history-event-1")).toHaveTextContent("AB12CD34");
    expect(screen.getByTestId("history-prev-change")).toHaveTextContent("无");
    expect(screen.getByTestId("history-next-change")).toHaveTextContent("AB12CD34");
    expect(m.state.lastAt).toBeInstanceOf(Date);
  });

  it("不传时刻时以当前时刻查询", async () => {
    render(<LookupPanel onSelectHandoff={vi.fn()} />);
    await lookupTube();
    fireEvent.click(screen.getByTestId("history-button"));
    await screen.findByTestId("history-result");
    expect(m.state.lastAt).toBeNull();
  });

  it("早于可追溯起点时明确提示无历史证据", async () => {
    const api = await import("../api");
    vi.mocked(api.api.getTubeHistory).mockImplementationOnce(async () =>
      m.history({
        point_in_time: "2026-09-16T17:00:00+08:00",
        evidence_available: false,
        custodian: null,
        event_seq: null,
        handoff_code: null,
        previous_change: null,
        next_change: null,
      }),
    );
    render(<LookupPanel onSelectHandoff={vi.fn()} />);
    await lookupTube();
    fireEvent.click(screen.getByTestId("history-button"));
    const note = await screen.findByTestId("history-no-evidence");
    expect(note).toHaveTextContent("没有任何历史证据");
    expect(screen.queryByTestId("history-custodian")).toBeNull();
  });

  it("断链错误：显示可识别错误且保留当前保管人查询结果", async () => {
    render(<LookupPanel onSelectHandoff={vi.fn()} />);
    await lookupTube();
    m.state.historyFailure = "broken";
    fireEvent.click(screen.getByTestId("history-button"));

    const err = await screen.findByTestId("history-error");
    expect(err).toHaveTextContent("custody_chain_broken");
    // 当前查询结果仍在
    expect(screen.getByTestId("lookup-custodian")).toHaveTextContent("S002");
    expect(screen.queryByTestId("history-result")).toBeNull();
  });

  it("缺基线错误：页面同样保留当前结果", async () => {
    render(<LookupPanel onSelectHandoff={vi.fn()} />);
    await lookupTube();
    m.state.historyFailure = "missing";
    fireEvent.click(screen.getByTestId("history-button"));
    await waitFor(() =>
      expect(screen.getByTestId("history-error")).toHaveTextContent("custody_baseline_missing"),
    );
    expect(screen.getByTestId("lookup-custodian")).toHaveTextContent("S002");
  });
});
