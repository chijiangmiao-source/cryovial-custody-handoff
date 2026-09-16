import { useState } from "react";
import { newOperationKey } from "../keystore";
import type { FieldErrors, Staff } from "../types";
import { useCommands } from "../useCommands";

function fieldText(fields: FieldErrors | undefined, pointer: string): string {
  return fields?.[pointer] ?? "";
}

export function CreatePanel({
  staff,
  onHandoff,
  commands,
}: {
  staff: Staff[];
  onHandoff: (code: string) => void;
  commands: ReturnType<typeof useCommands>;
}) {
  const [tubeCode, setTubeCode] = useState("T-1001");
  const [fromCode, setFromCode] = useState("S001");
  const [toCode, setToCode] = useState("S002");
  const [opKey, setOpKey] = useState(() => newOperationKey("create"));
  const [errors, setErrors] = useState<FieldErrors>({});

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setErrors({});
    const outcome = await commands.submit(
      "create",
      {
        tube_code: tubeCode.trim().toUpperCase(),
        from_staff_code: fromCode.trim().toUpperCase(),
        to_staff_code: toCode.trim().toUpperCase(),
        operation_key: opKey,
      },
    );
    if (outcome.ok && outcome.handoff) onHandoff(outcome.handoff.code);
    if (outcome.error && "fields" in outcome.error) {
      setErrors(outcome.error.fields ?? {});
    }
  }

  return (
    <form className="panel" onSubmit={submit} aria-label="发起交接">
      <h2>1. 转出员发起交接</h2>
      <label>
        冻存管码
        <input
          value={tubeCode}
          onChange={(e) => setTubeCode(e.target.value)}
          aria-label="管码"
          data-testid="tube-code-input"
        />
        <span className="field-error" role="alert">
          {fieldText(errors, "/tube_code")}
        </span>
      </label>
      <label>
        转出员工号（当前保管人）
        <select value={fromCode} onChange={(e) => setFromCode(e.target.value)} aria-label="转出员">
          {staff.map((s) => (
            <option key={s.code} value={s.code}>
              {s.code} · {s.name}
            </option>
          ))}
        </select>
        <span className="field-error" role="alert">
          {fieldText(errors, "/from_staff_code")}
        </span>
      </label>
      <label>
        目标接收员工号
        <select value={toCode} onChange={(e) => setToCode(e.target.value)} aria-label="接收员">
          {staff.map((s) => (
            <option key={s.code} value={s.code}>
              {s.code} · {s.name}
            </option>
          ))}
        </select>
        <span className="field-error" role="alert">
          {fieldText(errors, "/to_staff_code")}
        </span>
      </label>
      <label>
        操作键（断网/关页后保留用于重试）
        <input value={opKey} onChange={(e) => setOpKey(e.target.value)} aria-label="操作键" />
        <button type="button" className="link" onClick={() => setOpKey(newOperationKey("create"))}>
          重新生成
        </button>
        <span className="field-error" role="alert">
          {fieldText(errors, "/operation_key")}
        </span>
      </label>
      <button type="submit" disabled={commands.busyKey !== null} data-testid="create-button">
        生成交接码
      </button>
    </form>
  );
}
