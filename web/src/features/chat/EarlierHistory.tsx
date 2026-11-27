import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '../../api/client';
import { initialChatState, loadHistory, toggleSegment, type ChatMessage } from './chat';

export function EarlierHistory({ sessionId, archived, cursor, children }: {
  sessionId: string;
  archived: boolean;
  cursor: number;
  children: (message: ChatMessage, toggle: (id: string) => void) => ReactNode;
}) {
  const [before, setBefore] = useState<number | null>(cursor);
  const [history, setHistory] = useState(initialChatState);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const request = useRef<AbortController | null>(null);
  const content = useRef<HTMLDivElement>(null);
  const anchor = useRef<{ row: Element; list: Element; top: number } | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  useLayoutEffect(() => {
    const saved = anchor.current;
    if (saved?.row.isConnected) {
      saved.list.scrollTop += saved.row.getBoundingClientRect().top - saved.top;
    }
    anchor.current = null;
  }, [history.messages]);
  const toggle = useCallback((id: string) => setHistory(prev => toggleSegment(prev, id)), []);

  async function load() {
    if (before === null || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError('');
    try {
      const page = await api.earlierHistory(sessionId, before, archived, controller.signal);
      if (controller.signal.aborted || page.session_id !== sessionId) return;
      // 当前页已有会话摘要，每一页只追加自身的历史事件。
      const older = loadHistory(initialChatState, { ...page, earlier_cursor: null, summary: null }, archived);
      const row = content.current?.firstElementChild ?? content.current?.parentElement?.nextElementSibling;
      const list = content.current?.closest('.messages');
      if (row && list) anchor.current = { row, list, top: row.getBoundingClientRect().top };
      setHistory(prev => ({ ...prev, messages: [...older.messages, ...prev.messages] }));
      setBefore(page.earlier_cursor ?? null);
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Could not load earlier messages.');
    } finally {
      if (!controller.signal.aborted) {
        request.current = null;
        setLoading(false);
      }
    }
  }

  return <div className="history-pages">
    {before !== null && <div className="history-page-actions">
      <button type="button" className="chat-search-action" disabled={loading} onClick={() => void load()}>
        {loading ? 'Loading…' : 'Load earlier messages'}
      </button>
      {error && <span role="alert">{error}</span>}
    </div>}
    <div ref={content}>{history.messages.map(message => children(message, toggle))}</div>
  </div>;
}
