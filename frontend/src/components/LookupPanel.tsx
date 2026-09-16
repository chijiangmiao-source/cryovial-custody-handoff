import { useState } from "react";
import { ApiRequestError, NetworkFailure, api } from "../api";
import { CustodyTimeline } from "./CustodyTimeline";
import type { TubeHistory, TubeInfo } from "../types";

export function LookupPanel({ onSelectHandoff }: { onSelectHandoff: (code: string) => void }) {
  const [code, setCode] = useState("");
  const [tube, setTube] = useState<TubeInfo | null>(null);
  const [error, setError] = useState("");
  const [network, setNetwork] = useState(false);
  // datetime-local 的值（浏览器本地时区）；空串表示查询当前时刻
  const [point, setPoint] = useState("");
  const [history, setHistory] = useState<TubeHistory | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [historyNetwork, setHistoryNetwork] = useState(false);

  async function query(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setNetwork(false);
    setTube(null);
    setHistory(null);
    setHistoryError("");
    setHistoryNetwork(false);
    const c = code.trim().toUpperCase();
    if (!c) return;
    try {
      setTube(await api.getTube(c));
    } catch (err) {
      if (err instanceof ApiRequestError) setError(`${err.code}：${err.message}`);
      else if (err instanceof NetworkFailure) setNetwork(true);
    }
  }

  async function queryHistory(event: React.FormEvent) {
    event.preventDefault();
    setHistoryError("");
    setHistoryNetwork(false);
    const c = (tube?.code ?? code.trim().toUpperCase());
    if (!c) {
      setHistoryError("请先输入管码");
      return;
    }
    let at: Date | null = null;
    if (point) {
      at = new Date(point);
      if (Number.isNaN(at.getTime())) {
        setHistoryError("历史时刻格式无效");
        return;
      }
    }
    try {
      // 断链/缺基线返回可识别错误：失败时不清空 tube，页面保留当前查询结果
      setHistory(await api.getTubeHistory(c, at));
    } catch (err) {
      setHistory(null);
      if (err instanceof ApiRequestError) {
        if (err.code === "custody_baseline_missing" || err.code === "custody_chain_broken") {
          setHistoryError(`${err.code}：${err.message}（当前保管人查询结果仍保留在上方）`);
        } else {
          setHistoryError(`${err.code}：${err.message}`);
        }
      } else if (err instanceof NetworkFailure) {
        setHistoryNetwork(true);
      }
    }
  }

  return (
    <section className="panel" aria-label="状态查询">
      <h2>冻存管状态查询</h2>
      <form onSubmit={query} className="row">
        <input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="输入管码，如 T-1001"
          aria-label="查询管码"
          data-testid="lookup-input"
        />
        <button type="submit" data-testid="lookup-button">
          查询
        </button>
      </form>
      {tube && (
        <div data-testid="lookup-result">
          <p>
            {tube.code} 当前保管人：
            <strong data-testid="lookup-custodian">
              {tube.custodian.code} · {tube.custodian.name}
            </strong>
          </p>
          {tube.active_handoff ? (
            <button type="button" onClick={() => onSelectHandoff(tube.active_handoff!.code)}>
              打开活动交接 {tube.active_handoff.code}
            </button>
          ) : (
            <p className="hint">无活动交接</p>
          )}
        </div>
      )}
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      {network && (
        <p className="notice network" role="alert">
          网络不可用，请稍后重试
        </p>
      )}

      <div className="history-query">
        <h3>历史时间点查询（谁在该时刻保管）</h3>
        <form onSubmit={queryHistory} className="row">
          <input
            type="datetime-local"
            step="1"
            value={point}
            onChange={(e) => setPoint(e.target.value)}
            aria-label="历史时刻"
            data-testid="history-time-input"
          />
          <button type="submit" className="secondary" data-testid="history-button">
            查询该时刻
          </button>
          <button
            type="button"
            className="link"
            onClick={() => setPoint("")}
            data-testid="history-clear-time"
          >
            清空（查当前）
          </button>
        </form>
        <p className="hint">留空时刻即按数据库当前时刻投影；早于上线基线的时刻会明确提示无历史证据。</p>
        {history && <CustodyTimeline history={history} />}
        {historyError && (
          <p className="notice error" role="alert" data-testid="history-error">
            {historyError}
          </p>
        )}
        {historyNetwork && (
          <p className="notice network" role="alert" data-testid="history-network">
            网络不可用，请稍后重试（当前查询结果保留）
          </p>
        )}
      </div>
    </section>
  );
}
