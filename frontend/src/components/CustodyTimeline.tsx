import type { CustodyEventInfo, TubeHistory } from "../types";

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

function describeParty(e: CustodyEventInfo): string {
  if (e.kind === "baseline") return "系统上线基线（仅代表上线时现状）";
  return `${e.from_staff!.code} ${e.from_staff!.name} → ${e.to_staff!.code} ${e.to_staff!.name}`;
}

function ChangeItem({ label, event, testid }: { label: string; event: CustodyEventInfo | null; testid: string }) {
  if (!event) {
    return (
      <p className="history-change" data-testid={testid}>
        <span className="hint">{label}：</span>
        <span className="hint">无</span>
      </p>
    );
  }
  return (
    <p className="history-change" data-testid={testid}>
      <span className="hint">{label}：</span>
      <strong>#{event.seq}</strong> {fmtTime(event.effective_at)}
      {event.handoff_code ? ` · 交接码 ${event.handoff_code}` : " · 上线基线"} · 变更为{" "}
      {event.custodian.code} {event.custodian.name}
    </p>
  );
}

export function CustodyTimeline({ history }: { history: TubeHistory }) {
  return (
    <div data-testid="history-result">
      <p className="hint">
        可追溯起点（上线基线生效）：{fmtTime(history.traceable_since)}
      </p>

      {!history.evidence_available ? (
        <p className="notice conflict" role="alert" data-testid="history-no-evidence">
          {new Date(history.point_in_time).toLocaleString()} 早于可追溯起点，系统对该时刻的保管归属
          <strong>没有任何历史证据</strong>，不提供推断结果。
        </p>
      ) : (
        <div data-testid="history-point">
          <p>
            {new Date(history.point_in_time).toLocaleString()} 保管人：
            <strong data-testid="history-custodian">
              {history.custodian!.code} · {history.custodian!.name}
            </strong>
            （账本序号 <strong data-testid="history-seq">{history.event_seq}</strong>
            {history.handoff_code ? (
              <>
                ，对应交接码 <strong data-testid="history-handoff-code">{history.handoff_code}</strong>
              </>
            ) : (
              <>，上线基线，无交接码</>
            )}
            ）
          </p>
          <ChangeItem label="前一变更项" event={history.previous_change} testid="history-prev-change" />
          <ChangeItem label="后一变更项" event={history.next_change} testid="history-next-change" />
        </div>
      )}

      <ol className="timeline" aria-label="保管变更时间轴">
        {[...history.timeline].reverse().map((event) => (
          <li
            key={event.seq}
            className={event.active_at_point ? "timeline-item active" : "timeline-item"}
            data-testid={`history-event-${event.seq}`}
            data-active={event.active_at_point}
          >
            <div className="timeline-dot" aria-hidden="true" />
            <div>
              <p className="timeline-head">
                <strong>#{event.seq}</strong>
                {event.kind === "baseline" ? " 上线基线" : " 保管交接"}
                {event.active_at_point && <span className="badge completed">该时刻生效</span>}
              </p>
              <p className="timeline-time">{fmtTime(event.effective_at)}</p>
              <p>
                {describeParty(event)}
                {event.handoff_code && (
                  <>
                    {" · "}交接码 <span className="mono">{event.handoff_code}</span>
                  </>
                )}
              </p>
              <p className="hint">生效后保管人：{event.custodian.code} {event.custodian.name}</p>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
