import { useCallback, useEffect, useRef, useState } from 'react';
import { queueState } from '../../api/client';
import type { SessionInfo } from '../../types';

export function useSessionActivity(sessions: SessionInfo[], refreshSessions: () => Promise<void>) {
  const [replyingSessionIds, setReplyingSessionIds] = useState<ReadonlySet<string>>(() => new Set());
  const lastReplying = useRef(replyingSessionIds);
  const lastRequest = useRef(0);
  const latest = useRef<{ request: number; state: Awaited<ReturnType<typeof queueState>> }>();
  const readQueue = useCallback(async () => {
    const request = ++lastRequest.current;
    const atStart = latest.current;
    const state = await queueState().catch(error => {
      if (latest.current && request < latest.current.request) return latest.current.state;
      throw error;
    });
    const prior = latest.current;
    if (prior) {
      // 重叠请求在服务端的取样顺序可能相反。串行读取允许后端重启后从零计数。
      const comparable = prior !== atStart && prior.state.session_id === state.session_id
        && state.revision !== prior.state.revision;
      if (comparable ? state.revision < prior.state.revision : request < prior.request) {
        return { state: prior.state, stale: true };
      }
    }
    latest.current = { request: Math.max(request, prior?.request ?? 0), state };
    const ids = state.replying_session_ids;
    if (ids &&
        (ids.length !== lastReplying.current.size || ids.some(id => !lastReplying.current.has(id)))) {
      lastReplying.current = new Set(ids);
      setReplyingSessionIds(lastReplying.current);
      // 其他视图可能新建了聊天或完成了首个回合。
      void refreshSessions();
    }
    return { state, stale: false };
  }, [refreshSessions]);

  const hasPendingTitles = sessions.some(session => session.title_pending);
  useEffect(() => {
    if (!hasPendingTitles) return;
    let active = true;
    let timer = window.setTimeout(poll, 2000);
    async function poll() {
      await refreshSessions();
      if (active) timer = window.setTimeout(poll, 2000);
    }
    return () => { active = false; window.clearTimeout(timer); };
  }, [hasPendingTitles, refreshSessions]);

  return { replyingSessionIds, readQueue };
}
