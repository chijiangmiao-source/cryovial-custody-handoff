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
