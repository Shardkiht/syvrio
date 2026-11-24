import { useCallback, useEffect, useRef, useState } from 'react';
import { queueState } from '../api/client';
import type { SessionInfo } from '../types';

export function useSessionActivity(sessions: SessionInfo[], refreshSessions: () => Promise<void>) {
  const [replyingSessionIds, setReplyingSessionIds] = useState<ReadonlySet<string>>(() => new Set());
  const lastReplying = useRef(replyingSessionIds);
  const lastRequest = useRef(0);
  const readQueue = useCallback(async () => {
    const request = ++lastRequest.current;
    const state = await queueState();
    const ids = state.replying_session_ids;
    if (request === lastRequest.current && ids &&
        (ids.length !== lastReplying.current.size || ids.some(id => !lastReplying.current.has(id)))) {
      lastReplying.current = new Set(ids);
      setReplyingSessionIds(lastReplying.current);
      // 其他视图可能新建了聊天或完成了首个回合。
      void refreshSessions();
    }
    return state;
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
