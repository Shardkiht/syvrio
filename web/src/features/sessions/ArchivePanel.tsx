import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import { initialChatState, loadHistory, toggleSegment, type ChatState } from '../chat/chat';
import type { SessionInfo } from '../../types';
import { CommandDialog } from '../commands/CommandUI';
import { MessageList } from '../chat/MessageList';

export function ArchivePanel({ onClose, onRestore, onDelete, initialSelected }: {
  initialSelected?: SessionInfo;
  onClose: () => void;
  onRestore: () => Promise<void>;
  onDelete: (row: SessionInfo) => void;
}) {
  const [rows, setRows] = useState<SessionInfo[]>([]);
  const [selected, setSelected] = useState<SessionInfo | null>(initialSelected ?? null);
  const [history, setHistory] = useState<ChatState | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [restoring, setRestoring] = useState(false);
  useEffect(() => {
    let alive = true;
    api.listArchive().then(result => {
      if (alive) { setRows(result.items); setMore(result.items.length === 500); }
    })
      .catch(e => { if (alive) setError(e.message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    if (!selected) return;
    let alive = true;
    setHistory(null);
    api.archivedHistory(selected.id).then(result => { if (alive) setHistory(loadHistory(initialChatState, result, true)); })
      .catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [selected]);
  async function loadMore() {
    setLoading(true);
    try {
      const page = await api.listArchive(rows.length);
      setRows(previous => [...new Map([...previous, ...page.items].map(row => [row.id, row])).values()]);
      setMore(page.items.length === 500);
      setError('');
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not load more conversations.'); }
    finally { setLoading(false); }
  }
  async function restore() {
    if (!selected || restoring) return;
    setRestoring(true);
    try {
      await api.restoreSession(selected.id);
      setRows(previous => previous.filter(row => row.id !== selected.id));
      setSelected(null);
      setError('');
      await onRestore();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Restore failed.');
    } finally { setRestoring(false); }
  }
  return (
    <CommandDialog title="Archive" className="command-panel archive-panel" onClose={onClose}>
      <header className="pending-header">
        <strong>Archive</strong>
        <button type="button" className="icon-button" aria-label="Close Archive" onClick={onClose}>×</button>
      </header>
      {error && <div role="alert">{error}</div>}
      {selected ? <>
        <div className="archive-controls">
          <button type="button" onClick={() => { setSelected(null); setError(''); }}>Back</button>
          <strong>{selected.title}</strong>
        </div>
        {(history || !error) && <MessageList archived loading={!history}
          messages={history?.messages ?? initialChatState.messages} sessionId={selected.id}
          onToggle={id => setHistory(prev => prev && toggleSegment(prev, id))} />}
        <footer className="archive-controls">
          <button type="button" disabled={restoring} onClick={() => void restore()}>Restore to chats</button>
          <button type="button" className="archive-purge" autoFocus={!!initialSelected} disabled={restoring} onClick={() => onDelete(selected)}>Delete permanently…</button>
        </footer>
      </> : <div className="archive-list">
        {loading && !rows.length ? <p>Loading…</p> : !rows.length && !error && <p>No archived conversations.</p>}
        {rows.map(row => <button type="button" className="session" key={row.id}
          onClick={() => { setHistory(null); setSelected(row); setError(''); }}>{row.title}</button>)}
        {more && <button type="button" disabled={loading} onClick={() => void loadMore()}>Load more</button>}
      </div>}
    </CommandDialog>
  );
}
