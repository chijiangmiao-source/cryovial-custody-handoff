import type { Handoff, HandoffStatus } from "./types";

export const STATUS_TEXT: Record<HandoffStatus, string> = {
  pending: "待接受",
  accepted: "已接受",
  completed: "已完成",
  expired: "已到期",
};

// 业务错误码到页面语义（含“冲突”）
export function errorKind(code: string): "conflict" | "expired" | "forbidden" | "validation" | "other" {
  switch (code) {
    case "idempotency_conflict":
    case "active_handoff_exists":
    case "custodian_changed":
    case "not_custodian":
      return "conflict";
    case "handoff_expired":
      return "expired";
    case "not_receiver":
    case "not_owner":
      return "forbidden";
    case "validation_error":
      return "validation";
    default:
      return "other";
  }
}

export function formatFieldErrors(fields: Record<string, string> | undefined): string {
  if (!fields) return "";
  return Object.entries(fields)
    .map(([pointer, msg]) => `${pointer}：${msg}`)
    .join("；");
}

export function describeHandoff(h: Handoff): string {
  return `交接码 ${h.code}（管码 ${h.tube_code}）：${STATUS_TEXT[h.status]}，当前保管人 ${h.custodian_staff_code}`;
}
