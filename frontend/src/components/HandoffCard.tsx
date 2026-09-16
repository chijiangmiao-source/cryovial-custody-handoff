import type { Handoff } from "../types";
import { STATUS_TEXT } from "../statusLabels";

const TONE_CLASS: Record<string, string> = {
  pending: "badge pending",
  accepted: "badge accepted",
  completed: "badge completed",
  expired: "badge expired",
};

export function HandoffCard({ handoff }: { handoff: Handoff }) {
  return (
    <section className="card" aria-label="交接状态" data-testid="handoff-card">
      <div className="card-row">
        <strong data-testid="handoff-code">{handoff.code}</strong>
        <span className={TONE_CLASS[handoff.status]} data-testid="handoff-status">
          {STATUS_TEXT[handoff.status]}
        </span>
      </div>
      <dl className="grid">
        <dt>冻存管码</dt>
        <dd>{handoff.tube_code}</dd>
        <dt>转出员</dt>
        <dd>
          {handoff.from_staff.code} · {handoff.from_staff.name}
        </dd>
        <dt>接收员</dt>
        <dd>
          {handoff.to_staff.code} · {handoff.to_staff.name}
        </dd>
        <dt>当前保管人</dt>
        <dd data-testid="custodian">{handoff.custodian_staff_code}</dd>
        <dt>到期时刻</dt>
        <dd>{new Date(handoff.expires_at).toLocaleString()}</dd>
        <dt>剩余秒数</dt>
        <dd data-testid="seconds-remaining">{handoff.seconds_remaining}</dd>
      </dl>
    </section>
  );
}
