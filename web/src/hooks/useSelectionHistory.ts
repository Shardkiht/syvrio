import { useCallback, useRef, useState, type MutableRefObject } from 'react';
import { api, ApiError, NetworkError } from '../api/client';
import { initialChatState, loadHistory, toggleSegment, type ChatState } from '../state/chat';
import type { HistoryResponse } from '../types';

type Snapshot = { chat: ChatState; version?: string };
type Preview = { key: number; draftId: string; chat: ChatState; source?: Snapshot };
const MAX_CACHED_SESSIONS = 8;

export function useSelectionHistory(
  chat: MutableRefObject<ChatState>, version: MutableRefObject<number>,
  history: MutableRefObject<Promise<void> | null>, update: (fn: (prev: ChatState) => ChatState) => void,
  onRefresh: () => void,
) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [reading, setReading] = useState(false);
  const sequence = useRef(0);
  const committedKey = useRef(0);
  const blankDraftId = useRef('new/0');
  const active = useRef<Preview | null>(null);
  const cache = useRef(new Map<string, Snapshot>());
  const binding = useRef<string | null>(null);
  const remember = useCallback((data: HistoryResponse, version = data.history_version) => {
    const snapshot = { chat: loadHistory(initialChatState, data), version };
    cache.current.delete(data.session_id);
    cache.current.set(data.session_id, snapshot);
    if (cache.current.size > MAX_CACHED_SESSIONS) cache.current.delete(cache.current.keys().next().value!);
    return snapshot.chat;
  }, []);
  const forget = useCallback((id: string) => {
    cache.current.delete(id);
    if (binding.current === id) binding.current = null;
    if (active.current?.chat.sessionId === id) {
      active.current = { ...active.current, chat: { ...initialChatState, sessionId: id }, source: undefined };
      setPreview(active.current);
    }
  }, []);

  const startPreview = useCallback((id: string | null, waiting: boolean) => {
    const key = ++sequence.current;
    const draftId = id ?? (waiting && chat.current.sessionId === null ? `new/${key}` : blankDraftId.current);
    const cached = id === null ? undefined : cache.current.get(id);
    const view = { key, draftId, chat: cached?.chat ?? { ...initialChatState, sessionId: id }, source: cached };
    active.current = view;
    setPreview(view);
  }, [chat]);

  const switchHistory = useCallback(async (id: string) => {
    // Retain the exact snapshot sent for comparison, even if a separate refresh finishes in flight.
    binding.current = id;
    const intent = active.current?.key;
    const cached = cache.current.get(id);
    const request = cached?.version ? api.switchSession(id, cached.version) : api.switchSession(id);
    const result = await request.catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 404) forget(id);
      throw error;
    });
    if (result.session_id !== id) throw new NetworkError('Could not confirm the requested conversation.');
    if (binding.current !== id) return undefined; // An overlapping archive discarded this snapshot.
    if (result.history?.session_id === id) return remember(result.history, result.history_version);
    if (result.history === null && cached?.version && result.history_version === cached.version) {
      if (cache.current.get(id) === cached) {
        cache.current.delete(id);
        cache.current.set(id, cached);
      }
      const visible = active.current;
      return visible?.source === cached && visible.key === intent ? visible.chat : cached.chat;
    }
    // Older backends acknowledge the binding without returning a snapshot.
    return undefined;
  }, [forget, remember]);

  const finishPreview = useCallback((commit: boolean) => {
    const current = active.current;
    active.current = null;
    setPreview(null);
    if (commit) {
      committedKey.current = current?.key ?? ++sequence.current;
      if (current?.chat.sessionId === null) blankDraftId.current = current.draftId;
    }
    return current?.chat;
  }, []);

  const togglePreview = useCallback((segmentId: string) => {
    const view = active.current;
    if (!view) return;
    active.current = { ...view, chat: toggleSegment(view.chat, segmentId) };
    setPreview(active.current);
  }, []);

  const reloadHistory = useCallback(async (id: string | null) => {
    if (!id) return;
    const selection = version.current;
    try {
      const data = await api.history(id);
      if (selection === version.current && chat.current.sessionId === id && data.session_id === id) {
        update(() => remember(data));
        onRefresh();
      }
    } catch {
      // The currently displayed conversation remains usable after a failed refresh.
    }
  }, [chat, onRefresh, remember, update, version]);

  const loadSelectionHistory = useCallback((id: string) => {
    const revision = version.current;
    const current = () => revision === version.current && chat.current.sessionId === id;
    setReading(true);
    const loading = api.history(id).then(snapshot => {
      if (current() && !chat.current.busy && snapshot.session_id === id) update(() => remember(snapshot));
    }).catch((error: unknown) => {
      if (current()) update(prev => ({ ...prev, error: error instanceof Error ? error.message : '会话历史加载失败' }));
    }).finally(() => {
      if (history.current === loading) {
        history.current = null;
        setReading(false);
      }
    });
    history.current = loading;
  }, [chat, history, remember, update, version]);

  return { preview, startPreview, finishPreview, togglePreview, switchHistory, remember, forget, loadSelectionHistory, reloadHistory, committedKey, blankDraftId,
    loadingHistory: preview ? preview.chat.sessionId !== null && !preview.source : reading && history.current !== null };
}
