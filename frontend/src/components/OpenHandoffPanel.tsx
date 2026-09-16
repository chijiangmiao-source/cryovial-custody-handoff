import { useState } from "react";

// 交接码即凭证：转出员创建后可能关页或换设备，接收员凭码在此（或打开 ?code= 链接）继续交接
export function OpenHandoffPanel({
  current,
  onOpen,
}: {
  current: string;
  onOpen: (code: string) => void;
}) {
  const [code, setCode] = useState("");
  const [copied, setCopied] = useState(false);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const normalized = code.trim().toUpperCase();
    if (normalized) onOpen(normalized);
  }

  async function copyLink() {
    const link = shareLink(current);
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // 剪贴板不可用时降级为选中链接文本
    }
  }

  return (
    <section className="panel" aria-label="交接码入口">
      <h2>凭交接码继续 / 跨设备扫码</h2>
      <p className="hint">
        转出员可在创建后关页；接收员在任意设备输入或扫描交接码（或打开分享链接）即可接受，无需停留在发起页面。
      </p>
      <form className="row" onSubmit={submit}>
        <input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="输入/扫描交接码，如 ABCD2345"
          aria-label="交接码入口"
          data-testid="open-handoff-input"
          maxLength={16}
        />
        <button type="submit" data-testid="open-handoff-button">
          打开交接
        </button>
      </form>
      {current && (
        <div className="share" data-testid="share-box">
          <div className="mono" data-testid="share-link">
            {shareLink(current)}
          </div>
          <div className="row">
            <button type="button" className="secondary" onClick={copyLink} data-testid="copy-link">
              {copied ? "已复制" : "复制分享链接"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

export function shareLink(code: string): string {
  const loc = window.location;
  return `${loc.origin}${loc.pathname}?code=${encodeURIComponent(code)}`;
}

export function codeFromUrl(): string {
  const value = new URLSearchParams(window.location.search).get("code");
  return (value ?? "").trim().toUpperCase();
}
