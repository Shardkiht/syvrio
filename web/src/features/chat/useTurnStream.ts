import { useCallback, useEffect, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from 'react';
import { ApiError, replayTurn, type TurnReceipt } from '../../api/client';
import { reduceChat, startTurn, type ChatState } from './chat';
import type { CompactionPhase, StreamEvent } from '../../types';

type Options = {
  chat: MutableRefObject<ChatState>;
  selectionHistory: MutableRefObject<Promise<void> | null>;
  syncQueue: MutableRefObject<() => Promise<void>>;
  update: (fn: (state: ChatState) => ChatState) => void;
  reloadHistory: (id: string, reason: 'stream-ended') => Promise<void>;
  invalidateHistoryRefresh: () => void;
  refreshSessions: () => Promise<void>;
  setCompaction: Dispatch<SetStateAction<CompactionPhase | null>>;
  setOnline: (online: boolean) => void;
};

const terminal = new Set(['done', 'cancelled', 'failed']);

export function useTurnStream({ chat, selectionHistory, syncQueue, update, reloadHistory, invalidateHistoryRefresh,
  refreshSessions, setCompaction, setOnline }: Options) {
  const stream = useRef<{ controller: AbortController; turnId: number } | null>(null);
  // 只保留从提交到回执处理完毕的窗口。断流或切换会话都不代表生成结束。
  const deliveries = useRef(new Map<string, boolean>());
  const trackDelivery = useCallback((id: string) => {
    deliveries.current.set(id, false);
    return () => { deliveries.current.delete(id); };
  }, []);
  const detach = useCallback(() => {
    stream.current?.controller.abort();
    stream.current = null;
  }, []);
  useEffect(() => detach, [detach]);

  const observe = useCallback(async (receipt: TurnReceipt) => {
    if (stream.current || chat.current.sessionId !== receipt.session_id) return;
    if (receipt.submission_id && deliveries.current.get(receipt.submission_id)) return;
    const controller = new AbortController();
    const subscription = { controller, turnId: receipt.turn_id };
    stream.current = subscription;
    const current = () => !controller.signal.aborted && chat.current.sessionId === receipt.session_id;
    let seq = 0;
    let ended = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let pending: StreamEvent[] = [];
    const flush = () => {
      clearTimeout(timer);
      timer = undefined;
      const batch = pending;
      pending = [];
      if (current() && batch.length) update(prev => batch.reduce(reduceChat, prev));
    };
    const receive = (event: StreamEvent) => {
      if (!current() || event.seq <= seq) return;
      seq = event.seq;
      if (event.type === 'start' && event.turn_id != null) subscription.turnId = event.turn_id;
      if (event.type === 'compaction') {
        const phase = event.text;
        if (phase === 'running' || phase === 'done' || phase === 'failed') setCompaction(phase);
        return;
      }
      if (event.type === 'failed' || event.type === 'cancelled') {
        setCompaction(value => value === 'running' ? 'failed' : value);
      }
      ended = terminal.has(event.type);
      if (ended && receipt.submission_id && deliveries.current.has(receipt.submission_id)) {
        deliveries.current.set(receipt.submission_id, true);
      }
      pending.push(event);
      if (event.type === 'message_saved') {
        flush();
        void refreshSessions();
      } else if (timer === undefined) {
        timer = setTimeout(flush, 16);
      }
    };
    try {
      // 在所选会话的历史之上重放事件。视图之前切换到别处时，当前用户消息可能已经保存。
      if (selectionHistory.current) await selectionHistory.current;
      if (!current()) return;
      // 同一会话先前发出的历史刷新不能覆盖此回合，即使它在回合结束后才返回。
      invalidateHistoryRefresh();
      update(prev => {
        const saved = receipt.message_id
          ? prev.messages.findIndex(m => m.role === 'user' && m.savedId === receipt.message_id) : -1;
        const baseline = saved < 0 ? prev : { ...prev, messages: prev.messages.slice(0, saved) };
        return startTurn(baseline, receipt.text, receipt.session_id, receipt.message_id ?? undefined, receipt.submission_id);
      });
      while (current() && !ended) {
        try {
          await replayTurn(receipt.turn_id, seq, receive, controller.signal);
          if (!ended && current()) throw new Error('Stream interrupted');
        } catch (error) {
          if (!current() || error instanceof ApiError) break;
          setOnline(false);
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
      }
    } finally {
      flush();
      if (current()) {
        await reloadHistory(receipt.session_id, 'stream-ended');
        void refreshSessions();
        if (current()) setOnline(true);
      }
      if (stream.current?.controller === controller) {
        stream.current = null;
        if (current()) void syncQueue.current().catch(() => undefined);
      }
    }
  }, [chat, selectionHistory, syncQueue, refreshSessions, reloadHistory, invalidateHistoryRefresh, setCompaction, setOnline, update]);
  return { stream, detach, observe, trackDelivery };
}
