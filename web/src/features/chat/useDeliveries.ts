import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, NetworkError, newRequestId, submitChat, type TurnReceipt } from '../../api/client';
import { readEditFiles, removeEditFiles, saveEditFiles } from './editFiles';

export type Delivery = {
  id: string;
  sessionId: string | null;
  displaySessionId?: string;
  text: string;
  attachments: { id: string; name: string; media_type: string }[];
  files?: readonly File[];
  error?: string;
  reconnect?: boolean;
  receipt?: TurnReceipt;
};

const key = 'syvrio.outbox.v1';
function recover(): Delivery[] {
  try {
    const items: Delivery[] = JSON.parse(sessionStorage.getItem(key) ?? '[]');
    return items.filter(item => /^[a-f0-9]{32}$/.test(item.id) && typeof item.text === 'string'
      && (item.sessionId === null || typeof item.sessionId === 'string') && Array.isArray(item.attachments))
      .map(item => ({ ...item, error: item.error || 'Delivery unconfirmed. Retry to check.' }));
  } catch { return []; }
}

function transient(error: unknown) {
  return error instanceof NetworkError || (error instanceof ApiError
    && (error.code === 'backend_unavailable' || [502, 503, 504].includes(error.status ?? 0)));
}

type Schedule = (task: () => Promise<void>, onFailure: (error: unknown) => void) => Promise<void>;
const immediately: Schedule = (task, onFailure) => task().catch(onFailure);

export function useDeliveries(
  currentSession: () => string | null,
  accepted: (receipt: TurnReceipt) => Promise<void> | void,
  failed?: (error: unknown) => void,
  scheduleRetry: Schedule = immediately,
) {
  const [items, setItems] = useState<Delivery[]>(recover);
  const pending = useRef(items);
  const active = useRef(new Set<string>());
  const publish = useCallback((next: Delivery[]) => {
    pending.current = next;
    setItems(next);
    try {
      const unconfirmed = next.filter(item => !item.receipt);
      if (unconfirmed.length) sessionStorage.setItem(key, JSON.stringify(unconfirmed.map(({ files: _files, ...item }) => item)));
      else sessionStorage.removeItem(key);
    } catch { /* 已打开的标签页仍保留完整消息和文件。 */ }
  }, []);
  const discard = useCallback((id: string) => {
    const item = pending.current.find(row => row.id === id);
    if (!item) return;
    publish(pending.current.filter(item => item.id !== id));
    if (item.attachments.length) void removeEditFiles(id).catch(() => {});
  }, [publish]);

  const deliver = useCallback(async (item: Delivery, fresh: boolean, schedule: Schedule) => {
    if (active.current.has(item.id) || (!fresh && !pending.current.some(row => row.id === item.id && row.error))) return;
    active.current.add(item.id);
    const update = (value: Delivery) => publish(pending.current.map(row => row.id === item.id ? { ...row, ...value } : row));
    if (fresh) publish([...pending.current, item]);
    else update({ ...item, error: undefined });
    const backup = fresh && item.files?.length
      ? saveEditFiles(item.id, item.attachments.map(file => file.id), item.files).catch(() => {}) : undefined;
    const onFailure = (error: unknown) => {
      if (!pending.current.some(row => row.id === item.id)) return;
      update({ ...item, error: error instanceof Error ? error.message : 'Delivery failed.', reconnect: transient(error) });
      failed?.(error);
    };
    try {
      await schedule(async () => {
        // 前一条首发取得会话 ID 后，后续新请求才绑定它。重试始终沿用原请求参数。
        if (fresh && item.sessionId === null && currentSession() !== null) {
          item = { ...item, sessionId: currentSession() };
          update(item);
        } else if ((item.displaySessionId ?? item.sessionId) !== currentSession()) {
          throw new Error('Return to this conversation to retry delivery.');
        }
        await backup;
        const files = item.files ?? (item.attachments.length ? await readEditFiles(item.id, item.attachments) : []);
        const request = () => submitChat({ message: item.text, session_id: item.sessionId, submission_id: item.id }, files);
        let receipt: TurnReceipt;
        try {
          receipt = await request();
        } catch (error) {
          if (!transient(error)) throw error;
          await new Promise(resolve => setTimeout(resolve, 350));
          if (!pending.current.some(row => row.id === item.id)) return;
          receipt = await request();
        }
        if (!pending.current.some(row => row.id === item.id)) return;
        const sid = currentSession();
        if (sid !== null && sid !== receipt.session_id) { discard(item.id); return; }
        publish(pending.current.map(row => {
          if (row.id === item.id) return { ...row, displaySessionId: receipt.session_id, receipt, error: undefined };
          if (sid === null && row.sessionId === null && !row.displaySessionId) return { ...row, displaySessionId: receipt.session_id };
          return row;
        }));
        // 队列和历史接手之前保留已受理气泡，后续发送无需等待回放。
        void Promise.resolve().then(() => accepted(receipt))
          .catch(error => failed?.(error)).finally(() => discard(item.id));
      }, onFailure);
    } finally {
      active.current.delete(item.id);
    }
  }, [accepted, currentSession, discard, failed, publish]);

  const retry = useCallback(async (item: Delivery) => {
    if ((item.displaySessionId ?? item.sessionId) === currentSession()) await deliver(item, false, scheduleRetry);
  }, [currentSession, deliver, scheduleRetry]);

  useEffect(() => {
    const resume = async () => {
      for (const item of pending.current) {
        if (item.error && item.reconnect) await retry(item);
      }
    };
    window.addEventListener('online', resume);
    return () => window.removeEventListener('online', resume);
  }, [currentSession, retry]);

  const send = useCallback((sessionId: string | null, text: string, files: readonly File[], schedule = immediately) => {
    const id = newRequestId();
    return deliver({ id, sessionId, text, files,
      attachments: files.map((file, i) => ({ id: String(i), name: file.name, media_type: file.type })) }, true, schedule);
  }, [deliver]);
  return { items, send, retry, discard };
}
