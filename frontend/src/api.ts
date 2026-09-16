import type { ApiErrorBody, FieldErrors, Handoff, Staff, TubeInfo } from "./types";

export class ApiRequestError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public fields: FieldErrors = {},
    public replay: boolean = false,
  ) {
    super(message);
  }
}

// 断网、连接被拒、响应丢失等：调用方必须保留操作键并重试
export class NetworkFailure extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NetworkFailure";
  }
}

const RETRIABLE_STATUSES = new Set([408, 502, 503, 504]);

async function parseError(res: Response): Promise<ApiRequestError> {
  let body: ApiErrorBody | null = null;
  try {
    body = (await res.json()) as ApiErrorBody;
  } catch {
    body = null;
  }
  return new ApiRequestError(
    res.status,
    body?.error.code ?? "http_error",
    body?.error.message ?? `请求失败（HTTP ${res.status}）`,
    body?.error.fields ?? {},
    res.headers.get("x-idempotent-replay") === "true",
  );
}

export async function apiGet<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { Accept: "application/json" } });
  } catch (err) {
    throw new NetworkFailure(err instanceof Error ? err.message : "网络错误");
  }
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

interface CommandOptions {
  // 网络层失败时的自动重试次数；响应丢失不会重复执行业务（服务端按操作键重放）
  maxRetries?: number;
  retryDelayMs?: number;
  signal?: AbortSignal;
}

export async function postCommand<T>(
  path: string,
  payload: Record<string, unknown>,
  options: CommandOptions = {},
): Promise<{ data: T; replay: boolean; attempts: number }> {
  const maxRetries = options.maxRetries ?? 3;
  const retryDelayMs = options.retryDelayMs ?? 400;
  let attempt = 0;

  while (true) {
    attempt += 1;
    let res: Response;
    try {
      res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
        signal: options.signal,
      });
    } catch (err) {
      if (options.signal?.aborted) throw err;
      // 响应可能已到达服务端并执行：绝不能换新操作键，按原键重试以拿到首次结果
      if (attempt <= maxRetries) {
        await delay(retryDelayMs * attempt);
        continue;
      }
      throw new NetworkFailure(err instanceof Error ? err.message : "网络错误，操作键已保留，可重试");
    }

    if (RETRIABLE_STATUSES.has(res.status) && attempt <= maxRetries) {
      await delay(retryDelayMs * attempt);
      continue;
    }
    if (!res.ok) throw await parseError(res);
    const data = (await res.json()) as T;
    return {
      data,
      replay: res.headers.get("x-idempotent-replay") === "true",
      attempts: attempt,
    };
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export const api = {
  listStaff: () => apiGet<{ staff: Staff[] }>("/api/staff").then((r) => r.staff),
  getHandoff: (code: string) =>
    apiGet<{ handoff: Handoff }>(`/api/handoffs/${encodeURIComponent(code)}`).then((r) => r.handoff),
  getTube: (code: string) =>
    apiGet<{ tube: TubeInfo }>(`/api/tubes/${encodeURIComponent(code)}`).then((r) => r.tube),
  createHandoff: (payload: {
    tube_code: string;
    from_staff_code: string;
    to_staff_code: string;
    operation_key: string;
  }) =>
    postCommand<{ handoff: Handoff }>("/api/handoffs", payload).then((r) => ({
      handoff: r.data.handoff,
      replay: r.replay,
      attempts: r.attempts,
    })),
  acceptHandoff: (code: string, staff_code: string, operation_key: string) =>
    postCommand<{ handoff: Handoff }>(`/api/handoffs/${encodeURIComponent(code)}/accept`, {
      staff_code,
      operation_key,
    }).then((r) => ({ handoff: r.data.handoff, replay: r.replay, attempts: r.attempts })),
  confirmHandoff: (code: string, staff_code: string, operation_key: string) =>
    postCommand<{ handoff: Handoff }>(`/api/handoffs/${encodeURIComponent(code)}/confirm`, {
      staff_code,
      operation_key,
    }).then((r) => ({ handoff: r.data.handoff, replay: r.replay, attempts: r.attempts })),
};
