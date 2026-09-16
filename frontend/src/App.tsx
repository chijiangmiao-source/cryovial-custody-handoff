import { useCallback, useEffect, useState } from "react";
import { ApiRequestError, api } from "./api";
import { ConfirmPanel } from "./components/ConfirmPanel";
import { CreatePanel } from "./components/CreatePanel";
import { HandoffCard } from "./components/HandoffCard";
import { LookupPanel } from "./components/LookupPanel";
import { PendingCommands } from "./components/PendingCommands";
import { ScanPanel } from "./components/ScanPanel";
import type { Handoff, Staff } from "./types";
import { useCommands } from "./useCommands";

const NOTICE_CLASS: Record<string, string> = {
  success: "notice success",
  conflict: "notice conflict",
  expired: "notice expired",
  error: "notice error",
  network: "notice network",
};

export default function App() {
  const [staff, setStaff] = useState<Staff[]>([]);
  const [handoffCode, setHandoffCode] = useState("");
  const [handoff, setHandoff] = useState<Handoff | null>(null);
  const [loadError, setLoadError] = useState("");

  // 命令成功（含断网后重试）后同时定位交接码与状态，保证待处理命令重试也能打开扫码面板
  const onHandoff = useCallback((h: Handoff) => {
    setHandoff(h);
    setHandoffCode(h.code);
  }, []);
  const commands = useCommands(onHandoff);

  useEffect(() => {
    api
      .listStaff()
      .then(setStaff)
      .catch(() => setLoadError("人员名册加载失败，请检查后端服务"));
  }, []);

  const refreshHandoff = useCallback(async (code: string) => {
    if (!code) return;
    try {
      setHandoff(await api.getHandoff(code));
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 404) {
        setHandoff(null);
      }
      // 网络错误时保留上一次已知状态，不清空页面
    }
  }, []);

  useEffect(() => {
    if (!handoffCode) {
      setHandoff(null);
      return;
    }
    refreshHandoff(handoffCode);
    const timer = setInterval(() => refreshHandoff(handoffCode), 3000);
    return () => clearInterval(timer);
  }, [handoffCode, refreshHandoff]);

  // 命令成功后立即拉取最新状态，避免等待轮询
  useEffect(() => {
    if (commands.notice?.tone === "success" && handoffCode) refreshHandoff(handoffCode);
  }, [commands.notice, handoffCode, refreshHandoff]);

  return (
    <main className="page">
      <header>
        <h1>冷冻样本库 · 夜班冻存管交接</h1>
        <p className="subtitle">
          每管最多一个活动交接 · 操作键重放首次结果 · 到期只封闭不夺管 · 保管人与完成状态原子写入
        </p>
      </header>

      {loadError && (
        <p className="notice error" role="alert">
          {loadError}
        </p>
      )}
      {commands.notice && (
        <p className={NOTICE_CLASS[commands.notice.tone]} role="status" data-testid="notice">
          {commands.notice.text}
        </p>
      )}

      <div className="layout">
        <CreatePanel staff={staff} onHandoff={setHandoffCode} commands={commands} />
        {handoffCode ? (
          <ScanPanel
            staff={staff}
            handoffCode={handoffCode}
            onAccepted={() => refreshHandoff(handoffCode)}
            commands={commands}
          />
        ) : (
          <section className="panel muted">
            <h2>2. 接收员扫码接受</h2>
            <p className="hint">发起交接后在此扫码。</p>
          </section>
        )}
        <ConfirmPanel handoff={handoff} onCompleted={() => refreshHandoff(handoffCode)} commands={commands} />
      </div>

      {handoff && <HandoffCard handoff={handoff} />}

      <PendingCommands commands={commands} />
      <LookupPanel onSelectHandoff={setHandoffCode} />

      <footer className="hint">
        交接码创建 10 分钟后到期；截止时刻仍有效，其后仅封闭未完成交接，不改变保管人。
      </footer>
    </main>
  );
}
