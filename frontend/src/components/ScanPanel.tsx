import { useState } from "react";
import { newOperationKey } from "../keystore";
import type { FieldErrors, Staff } from "../types";
import { useCommands } from "../useCommands";

export function ScanPanel({
  staff,
  handoffCode,
  onAccepted,
  commands,
}: {
  staff: Staff[];
  handoffCode: string;
  onAccepted: () => void;
  commands: ReturnType<typeof useCommands>;
}) {
  const [staffCode, setStaffCode] = useState("S002");
  const [opKey, setOpKey] = useState(() => newOperationKey("accept"));
  const [errors, setErrors] = useState<FieldErrors>({});

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!handoffCode) return;
    setErrors({});
    const outcome = await commands.submit(
      "accept",
      { staff_code: staffCode.trim().toUpperCase(), operation_key: opKey },
      handoffCode,
    );
    if (outcome.ok) onAccepted();
    if (outcome.error && "fields" in outcome.error) {
      setErrors(outcome.error.fields ?? {});
    }
  }

  return (
    <form className="panel" onSubmit={submit} aria-label="扫码接受">
      <h2>2. 接收员扫码接受</h2>
      <label>
        交接码（扫码枪输入）
        <input
          value={handoffCode}
          readOnly
          aria-label="交接码"
          data-testid="accept-code"
        />
      </label>
      <label>
        接收员工号
        <select value={staffCode} onChange={(e) => setStaffCode(e.target.value)} aria-label="扫码员工号">
          {staff.map((s) => (
            <option key={s.code} value={s.code}>
              {s.code} · {s.name}
            </option>
          ))}
        </select>
        <span className="field-error" role="alert">
          {errors["/staff_code"] ?? ""}
        </span>
      </label>
      <label>
        操作键（扫码器重发时保持不变）
        <input value={opKey} onChange={(e) => setOpKey(e.target.value)} aria-label="接受操作键" />
        <button type="button" className="link" onClick={() => setOpKey(newOperationKey("accept"))}>
          重新生成
        </button>
      </label>
      <button type="submit" disabled={!handoffCode || commands.busyKey !== null} data-testid="accept-button">
        扫码接受
      </button>
    </form>
  );
}
