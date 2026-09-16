import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiRequestError, NetworkFailure, postCommand } from "./api";

afterEach(() => vi.restoreAllMocks());

function jsonResponse(body: unknown, init: ResponseInit = {}, replay = false): Response {
  const headers = new Headers(init.headers);
  if (replay) headers.set("x-idempotent-replay", "true");
  return new Response(JSON.stringify(body), { ...init, headers });
}

const handoffBody = {
  handoff: {
    code: "ABC12345",
    tube_code: "T-1001",
    from_staff: { code: "S001", name: "张" },
    to_staff: { code: "S002", name: "李" },
    status: "pending",
    custodian_staff_code: "S001",
    created_at: "2026-01-01T00:00:00+00:00",
    expires_at: "2026-01-01T00:10:00+00:00",
    accepted_at: null,
    completed_at: null,
    expired: false,
    seconds_remaining: 600,
  },
};

describe("postCommand", () => {
  it("网络失败后用同一操作键重试，最终成功且不换键", async () => {
    const calls: string[] = [];
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => {
        calls.push("fail");
        return Promise.reject(new TypeError("Failed to fetch"));
      })
      .mockImplementationOnce((_url, init) => {
        calls.push(JSON.parse(init.body).operation_key);
        return Promise.resolve(jsonResponse(handoffBody, { status: 201 }));
      });
    vi.stubGlobal("fetch", fetchMock);

    const res = await postCommand(
      "/api/handoffs",
      { tube_code: "T-1001", operation_key: "create-fixed-key-0001" },
      { retryDelayMs: 1 },
    );

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(calls).toEqual(["fail", "create-fixed-key-0001"]);
    expect(res.data).toEqual(handoffBody);
    expect(res.attempts).toBe(2);
  });

  it("重试耗尽后抛 NetworkFailure（调用方保留操作键）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("network down")),
    );
    await expect(
      postCommand("/api/handoffs/x/accept", { operation_key: "accept-key-0001" }, { maxRetries: 1, retryDelayMs: 1 }),
    ).rejects.toBeInstanceOf(NetworkFailure);
  });

  it("503 后重试；成功响应的重放头被透传", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("bad gateway", { status: 503 }))
      .mockResolvedValueOnce(jsonResponse({ handoff: { ...handoffBody.handoff, status: "accepted" } }, { status: 200 }, true));
    vi.stubGlobal("fetch", fetchMock);

    const res = await postCommand(
      "/api/handoffs/ABC12345/accept",
      { staff_code: "S002", operation_key: "accept-key-0002" },
      { retryDelayMs: 1 },
    );
    expect(res.replay).toBe(true);
    expect((res.data as any).handoff.status).toBe("accepted");
  });

  it("业务错误抛 ApiRequestError 并保留 JSON Pointer 字段", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: "active_handoff_exists",
              message: "已有进行中的交接",
              fields: { "/tube_code": "存在未完成的交接" },
            },
          },
          { status: 409 },
        ),
      ),
    );
    await expect(
      postCommand("/api/handoffs", { operation_key: "k" }, { retryDelayMs: 1 }),
    ).rejects.toMatchObject({
      status: 409,
      code: "active_handoff_exists",
      fields: { "/tube_code": "存在未完成的交接" },
    });
  });

  it("同键异参 409 归类为冲突错误", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          { error: { code: "idempotency_conflict", message: "冲突", fields: { "/operation_key": "x" } } },
          { status: 409 },
        ),
      ),
    );
    await expect(api.createHandoff({
      tube_code: "T-1",
      from_staff_code: "S001",
      to_staff_code: "S002",
      operation_key: "create-conflict-key",
    })).rejects.toBeInstanceOf(ApiRequestError);
  });
});
