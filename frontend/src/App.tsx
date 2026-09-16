import { useCallback, useEffect, useState } from "react";
import { ApiRequestError, api } from "./api";
import { ConfirmPanel } from "./components/ConfirmPanel";
import { CreatePanel } from "./components/CreatePanel";
import { HandoffCard } from "./components/HandoffCard";
import { LookupPanel } from "./components/LookupPanel";
import { OpenHandoffPanel, codeFromUrl } from "./components/OpenHandoffPanel";
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

function syncUrl(code: string) {
  const url = new URL(window.location.href);
  if (code) url.searchParams.set("code", code);
  else url.searchParams.delete("code");
  window.history.replaceState(null, "", url);
}

export default function App() {
  // 支持跨设备：接收员可直接打开 ?code=XXXX 链接进入扫码接受
  const [handoffCode, setHandoffCode] = useState(() => codeFromUrl());
  const [handoff, setHandoff] = useState<Handoff | null>(null);
  const [staff, setStaff] = useState<Staff[]>([]);
  const [loadError, setLoadError] = useState("");
  const [openError, setOpenError] = useState("");

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

  const openHandoff = useCallback((code: string) => {
    setOpenError("");
    setHandoffCode(code);
  }, []);

  const refreshHandoff = useCallback(
    async (code: string) => {
      if (!code) return;
      try {
        setHandoff(await api.getHandoff(code));
        setOpenError("");
      } catch (err) {
        if (err instanceof ApiRequestError && err.status === 404) {
          setHandoff(null);
          setOpenError(`交接码 ${code} 不存在，请确认扫码内容`);
        }
        // 网络错误时保留上一次已知状态，不清空页面
      }
    },
    [],
  );

  // 交接码变化时同步到地址栏，便于把链接发给接收员在其他设备打开
  useEffect(() => {
    syncUrl(handoffCode);
  }, [handoffCode]);

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

      <OpenHandoffPanel current={handoffCode} onOpen={openHandoff} />
      {openError && (
        <p className="notice error" role="alert" data-testid="open-error">
          {openError}
        </p>
      )}

      <div className="layout">
        <CreatePanel staff={staff} onHandoff={openHandoff} commands={commands} />
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
            <p className="hint">在上方输入/扫描交接码，或打开转出员分享的链接后在此接受。</p>
          </section>
        )}
        <ConfirmPanel handoff={handoff} onCompleted={() => refreshHandoff(handoffCode)} commands={commands} />
      </div>

      {handoff && <HandoffCard handoff={handoff} />}

      <PendingCommands commands={commands} />
      <LookupPanel onSelectHandoff={openHandoff} />

      <footer className="hint">
        交接码创建 10 分钟后到期；截止时刻仍有效，其后仅封闭未完成交接，不改变保管人。
      </footer>
    </main>
  );
}
