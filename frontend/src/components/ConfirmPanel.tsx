import { useState } from "react";
import { newOperationKey } from "../keystore";
import type { Handoff } from "../types";
import { useCommands } from "../useCommands";

export function ConfirmPanel({
  handoff,
  onCompleted,
  commands,
}: {
  handoff: Handoff | null;
  onCompleted: () => void;
  commands: ReturnType<typeof useCommands>;
}) {
  const [staffCode, setStaffCode] = useState("S001");
  const [opKey, setOpKey] = useState(() => newOperationKey("confirm"));
  const [errorText, setErrorText] = useState("");

  const canAttempt = handoff !== null && handoff.status === "accepted";

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!handoff) return;
    setErrorText("");
    const outcome = await commands.submit(
      "confirm",
      { staff_code: staffCode.trim().toUpperCase(), operation_key: opKey },
      handoff.code,
    );
    if (outcome.ok) onCompleted();
    if (outcome.error) {
      setErrorText(outcome.error.message);
    }
  }

  return (
    <form className="panel" onSubmit={submit} aria-label="最终确认">
      <h2>3. 转出员最终确认</h2>
      <p className="hint">
        仅当接收员已接受、且转出员仍为当前保管人时方可确认；保管人切换与完成状态在同一事务原子写入。
      </p>
      <label>
        确认人工号（须为当前保管人的转出员）
        <input
          value={staffCode}
          onChange={(e) => setStaffCode(e.target.value)}
          aria-label="确认员工号"
          data-testid="confirm-staff"
        />
      </label>
      <label>
        操作键（响应丢失后用同一键重试，服务端重放首次结果）
        <input value={opKey} onChange={(e) => setOpKey(e.target.value)} aria-label="确认操作键" />
        <button type="button" className="link" onClick={() => setOpKey(newOperationKey("confirm"))}>
          重新生成
        </button>
      </label>
      {errorText && (
        <p className="field-error" role="alert" data-testid="confirm-error">
          {errorText}
        </p>
      )}
      <button type="submit" disabled={!canAttempt || commands.busyKey !== null} data-testid="confirm-button">
        最终确认
      </button>
      {handoff && handoff.status !== "accepted" && (
        <p className="hint" data-testid="confirm-blocked">
          当前状态“{handoff.status}”不可确认
        </p>
      )}
    </form>
  );
}
