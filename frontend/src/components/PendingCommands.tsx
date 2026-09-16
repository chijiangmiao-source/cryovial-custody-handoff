import type { StoredCommand } from "../types";
import type { useCommands } from "../useCommands";

const KIND_TEXT: Record<StoredCommand["kind"], string> = {
  create: "发起交接",
  accept: "扫码接受",
  confirm: "最终确认",
};

export function PendingCommands({ commands }: { commands: ReturnType<typeof useCommands> }) {
  if (commands.pending.length === 0) return null;

  return (
    <section className="panel pending" aria-label="待处理操作" data-testid="pending-commands">
      <h2>断网/响应丢失后的待处理操作</h2>
      <p className="hint">以下命令的操作键已保留；网络恢复后点击重试，服务端按操作键重放首次结果，不会重复执行。</p>
      <ul>
        {commands.pending.map((cmd) => (
          <li key={cmd.key} className="pending-item">
            <div>
              <strong>{KIND_TEXT[cmd.kind]}</strong>
              {cmd.handoffCode ? <span> · 交接码 {cmd.handoffCode}</span> : null}
              <div className="mono">键：{cmd.key}</div>
            </div>
            <div className="row">
              <button
                type="button"
                onClick={() => commands.retry(cmd)}
                disabled={commands.busyKey !== null}
                data-testid={`retry-${cmd.kind}`}
              >
                重试
              </button>
              <button type="button" className="secondary" onClick={() => commands.dismiss(cmd.key)}>
                移除
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
