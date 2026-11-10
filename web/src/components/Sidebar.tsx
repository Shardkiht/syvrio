import type { SessionInfo } from '../types';

export function Sidebar({
  sessions,
  currentId,
  open,
  model,
  onNew,
  onSelect,
  onDelete,
  onClose,
}: {
  sessions: SessionInfo[];
  currentId: string | null;
  open: boolean;
  model: string;
  onNew: () => void;
  onSelect: (sessionId: string) => void;
  onDelete: (session: SessionInfo) => void;
  onClose: () => void;
}) {
  return (
    <>
      {open && <div className="sidebar-scrim" onClick={onClose} data-testid="sidebar-scrim" />}
      <aside className={`sidebar${open ? ' open' : ''}`} data-testid="sidebar">
        <div className="sidebar-head">
          <span className="brand">Syvrio</span>
          <button type="button" className="sidebar-close" onClick={onClose} aria-label="Close sessions">
            «
          </button>
        </div>
        <button type="button" className="new-chat" onClick={onNew}>New chat</button>
        <div className="session-list">
          {sessions.length === 0 && <div className="empty">No conversations yet</div>}
          {sessions.map((session) => (
            <div
              key={session.id}
              className={`session-row${session.id === currentId ? ' active' : ''}`}
            >
              <button
                type="button"
                className="session"
                onClick={() => onSelect(session.id)}
                data-testid={`session-${session.id}`}
              >
                <span className="session-title">{session.title}</span>
                <span className="session-meta">{session.messages} messages</span>
              </button>
              <button
                type="button"
                className="session-delete"
                onClick={() => onDelete(session)}
                aria-label={`Delete ${session.title}`}
                title="Delete session"
                data-testid={`delete-${session.id}`}
              >
                ×
              </button>
            </div>
          ))}
        </div>
        <div className="sidebar-foot" title={model}>{model}</div>
      </aside>
    </>
  );
}
