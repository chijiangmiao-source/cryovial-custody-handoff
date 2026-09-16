import { useState } from "react";
import { ApiRequestError, NetworkFailure, api } from "../api";
import type { TubeInfo } from "../types";

export function LookupPanel({ onSelectHandoff }: { onSelectHandoff: (code: string) => void }) {
  const [code, setCode] = useState("");
  const [tube, setTube] = useState<TubeInfo | null>(null);
  const [error, setError] = useState("");
  const [network, setNetwork] = useState(false);

  async function query(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setNetwork(false);
    setTube(null);
    const c = code.trim().toUpperCase();
    if (!c) return;
    try {
      setTube(await api.getTube(c));
    } catch (err) {
      if (err instanceof ApiRequestError) setError(`${err.code}：${err.message}`);
      else if (err instanceof NetworkFailure) setNetwork(true);
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
    </section>
  );
}
