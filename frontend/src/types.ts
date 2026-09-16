export type HandoffStatus = "pending" | "accepted" | "completed" | "expired";

export interface Staff {
  code: string;
  name: string;
}

export interface Handoff {
  code: string;
  tube_code: string;
  from_staff: Staff;
  to_staff: Staff;
  status: HandoffStatus;
  custodian_staff_code: string;
  created_at: string;
  expires_at: string;
  accepted_at: string | null;
  completed_at: string | null;
  expired: boolean;
  seconds_remaining: number;
}

export interface TubeInfo {
  code: string;
  custodian: Staff;
  active_handoff: Handoff | null;
}

export interface CustodyEventInfo {
  seq: number;
  kind: "baseline" | "transfer";
  effective_at: string;
  custodian: Staff;
  from_staff: Staff | null;
  to_staff: Staff | null;
  handoff_code: string | null;
  active_at_point: boolean;
}

export interface TubeHistory {
  tube_code: string;
  queried_at: string;
  point_in_time: string;
  // 上线基线（可追溯起点）；早于该时刻 evidence_available=false
  traceable_since: string;
  // false 表示查询时刻早于可追溯起点：没有任何历史证据，不猜测保管人
  evidence_available: boolean;
  custodian: Staff | null;
  event_seq: number | null;
  handoff_code: string | null;
  previous_change: CustodyEventInfo | null;
  next_change: CustodyEventInfo | null;
  timeline: CustodyEventInfo[];
}

export interface FieldErrors {
  [pointer: string]: string;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    fields?: FieldErrors;
  };
}

export type CommandKind = "create" | "accept" | "confirm";

export interface StoredCommand {
  kind: CommandKind;
  key: string;
  payload: Record<string, unknown>;
  handoffCode?: string;
  createdAt: string;
  resolved?: boolean;
  lastStatus?: number;
}
