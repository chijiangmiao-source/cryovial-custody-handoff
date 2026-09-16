import { useCallback, useEffect, useState } from "react";
import { api, ApiRequestError, NetworkFailure } from "./api";
import {
  listCommands,
  markResolved,
  removeCommand,
  saveCommand,
} from "./keystore";
import type { CommandKind, Handoff, StoredCommand } from "./types";

export interface Notice {
  tone: "success" | "conflict" | "expired" | "error" | "network";
  text: string;
}

export interface SubmitOutcome {
  ok: boolean;
  handoff?: Handoff;
  replay?: boolean;
  error?: ApiRequestError | NetworkFailure;
}

function executeStored(cmd: StoredCommand) {
  const p = cmd.payload as {
    tube_code?: string;
    from_staff_code?: string;
    to_staff_code?: string;
    staff_code?: string;
    operation_key: string;
  };
  switch (cmd.kind) {
    case "create":
      return api.createHandoff({
        tube_code: p.tube_code!,
        from_staff_code: p.from_staff_code!,
        to_staff_code: p.to_staff_code!,
        operation_key: p.operation_key,
      });
    case "accept":
      return api.acceptHandoff(cmd.handoffCode!, p.staff_code!, p.operation_key);
    case "confirm":
      return api.confirmHandoff(cmd.handoffCode!, p.staff_code!, p.operation_key);
  }
}

export function useCommands(onHandoff?: (h: Handoff) => void) {
  const [pending, setPending] = useState<StoredCommand[]>(() =>
    listCommands().filter((c) => !c.resolved),
  );
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const refresh = useCallback(() => {
    setPending(listCommands().filter((c) => !c.resolved));
  }, []);

  useEffect(() => {
    const handler = () => refresh();
    window.addEventListener("pending-commands-changed", handler);
    window.addEventListener("storage", handler);
    return () => {
      window.removeEventListener("pending-commands-changed", handler);
      window.removeEventListener("storage", handler);
    };
  }, [refresh]);

  const settle = useCallback(
    async (cmd: StoredCommand): Promise<SubmitOutcome> => {
      setBusyKey(cmd.key);
      try {
        const res = await executeStored(cmd);
        markResolved(cmd.key, 200, res.handoff.code);
        onHandoff?.(res.handoff);
        setNotice({
          tone: "success",
          text: `${res.replay ? "重放首次结果：" : ""}${res.handoff.code} → ${res.handoff.status}（尝试 ${res.attempts} 次）`,
        });
        refresh();
        return { ok: true, handoff: res.handoff, replay: res.replay };
      } catch (err) {
        if (err instanceof ApiRequestError) {
          // 服务端已记录该操作键的首次结果：不再是待处理命令
          markResolved(cmd.key, err.status, cmd.handoffCode);
          const tone =
            err.code === "handoff_expired"
              ? "expired"
              : err.code === "idempotency_conflict" ||
                  err.code === "active_handoff_exists" ||
                  err.code === "custodian_changed"
                ? "conflict"
                : "error";
          setNotice({ tone, text: err.message });
          refresh();
          return { ok: false, error: err };
        }
        // NetworkFailure：保留操作键，留在待处理列表等待重试
        setNotice({ tone: "network", text: "网络中断或响应丢失，操作键已保留，可直接重试" });
        return { ok: false, error: err as NetworkFailure };
      } finally {
        setBusyKey(null);
      }
    },
    [onHandoff, refresh],
  );

  const submit = useCallback(
    async (
      kind: CommandKind,
      payload: Record<string, unknown>,
      handoffCode?: string,
    ): Promise<SubmitOutcome> => {
      const cmd: StoredCommand = {
        kind,
        key: String(payload.operation_key),
        payload,
        handoffCode,
        createdAt: new Date().toISOString(),
      };
      saveCommand(cmd);
      refresh();
      return settle(cmd);
    },
    [refresh, settle],
  );

  const retry = useCallback((cmd: StoredCommand) => settle(cmd), [settle]);

  const dismiss = useCallback(
    (key: string) => {
      removeCommand(key);
      refresh();
    },
    [refresh],
  );

  return { pending, notice, setNotice, busyKey, submit, retry, dismiss };
}
