// 操作键的本地持久化：断网、关页或响应丢失后，页面仍能找到原操作键并重放同一命令。
import type { StoredCommand } from "./types";

const STORAGE_KEY = "handoff.pendingCommands.v1";

function readAll(): StoredCommand[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as StoredCommand[]) : [];
  } catch {
    return [];
  }
}

function writeAll(commands: StoredCommand[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(commands));
  window.dispatchEvent(new CustomEvent("pending-commands-changed"));
}

export function listCommands(): StoredCommand[] {
  return readAll();
}

export function saveCommand(command: StoredCommand): void {
  const all = readAll().filter((c) => c.key !== command.key);
  all.push(command);
  writeAll(all);
}

export function markResolved(key: string, status: number, handoffCode?: string): void {
  const all = readAll();
  const idx = all.findIndex((c) => c.key === key);
  if (idx >= 0) {
    all[idx] = { ...all[idx], resolved: true, lastStatus: status, handoffCode: handoffCode ?? all[idx].handoffCode };
    writeAll(all);
  }
}

export function removeCommand(key: string): void {
  writeAll(readAll().filter((c) => c.key !== key));
}

export function newOperationKey(kind: string): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().replace(/-/g, "")
      : Math.random().toString(36).slice(2) + Date.now().toString(36);
  return `${kind}-${random}`;
}
