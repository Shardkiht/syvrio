import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, NetworkError, newRequestId, submitChat, type TurnReceipt } from '../api/client';
import { readEditFiles, removeEditFiles, saveEditFiles } from '../state/editFiles';

type Delivery = {
  id: string;
  sessionId: string | null;
  text: string;
  attachments: { id: string; name: string; media_type: string }[];
  files?: readonly File[];
  error?: string;
  reconnect?: boolean;
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

const immediately = (task: () => Promise<void>) => task();

export function useDeliveries(
  currentSession: () => string | null,
  accepted: (receipt: TurnReceipt) => Promise<void> | void,
  failed?: (error: unknown) => void,
  scheduleRetry = immediately,
) {
  const [items, setItems] = useState<Delivery[]>(recover);
  const pending = useRef(items);
  const active = useRef(new Set<string>());
  const publish = useCallback((next: Delivery[]) => {
    pending.current = next;
    setItems(next);
    try {
      if (next.length) sessionStorage.setItem(key, JSON.stringify(next.map(({ files: _files, ...item }) => item)));
      else sessionStorage.removeItem(key);
    } catch { /* 已打开的标签页仍保留完整消息和文件。 */ }
  }, []);
  const discard = useCallback((id: string) => {
    const hasFiles = pending.current.some(item => item.id === id && item.attachments.length > 0);
    publish(pending.current.filter(item => item.id !== id));
    if (hasFiles) void removeEditFiles(id).catch(() => {});
  }, [publish]);

  const deliver = useCallback(async (item: Delivery, fresh = false) => {
    if (active.current.has(item.id)) return;
    active.current.add(item.id);
    const update = (value: Delivery) => publish(pending.current.map(row => row.id === item.id ? value : row));
    if (fresh) publish([...pending.current, item]);
    else update({ ...item, error: undefined });
    try {
      if (fresh && item.files?.length) {
        await saveEditFiles(item.id, item.attachments.map(file => file.id), item.files).catch(() => {});
      }
      const files = item.files ?? (item.attachments.length ? await readEditFiles(item.id, item.attachments) : []);
      const request = () => submitChat({ message: item.text, session_id: item.sessionId, submission_id: item.id }, files);
      let receipt: TurnReceipt;
      try {
        receipt = await request();
      } catch (error) {
        if (!transient(error)) throw error;
        await new Promise(resolve => setTimeout(resolve, 350));
        receipt = await request();
      }
      discard(item.id);
      const sid = currentSession();
      if (sid === item.sessionId || sid === receipt.session_id) await accepted(receipt);
    } catch (error) {
      if (pending.current.some(row => row.id === item.id)) {
        update({ ...item, error: error instanceof Error ? error.message : 'Delivery failed.', reconnect: transient(error) });
        failed?.(error);
      }
    } finally {
      active.current.delete(item.id);
    }
  }, [accepted, currentSession, discard, failed, publish]);

  const retry = useCallback((item: Delivery) => scheduleRetry(async () => {
    if (item.sessionId === currentSession() && pending.current.some(row => row.id === item.id)) {
      await deliver(item);
    }
  }), [currentSession, deliver, scheduleRetry]);

  useEffect(() => {
    const resume = async () => {
      for (const item of pending.current) {
        if (item.error && item.reconnect && item.sessionId === currentSession()) await retry(item);
      }
    };
    window.addEventListener('online', resume);
    return () => window.removeEventListener('online', resume);
  }, [currentSession, retry]);

  const send = useCallback((sessionId: string | null, text: string, files: readonly File[]) => {
    const id = newRequestId();
    return deliver({ id, sessionId, text, files,
      attachments: files.map((file, i) => ({ id: String(i), name: file.name, media_type: file.type })) }, true);
  }, [deliver]);
  return { items, send, retry, discard };
}
