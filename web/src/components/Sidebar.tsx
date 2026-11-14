import type { SessionInfo } from '../types';
import { PanelIcon, PlusIcon } from './Icons';

export function Sidebar({
  sessions,
  currentId,
  open,
  model,
  online,
  onNew,
  onSelect,
  onDelete,
  onClose,
}: {
  sessions: SessionInfo[];
  currentId: string | null;
  open: boolean;
  model: string;
  online: boolean;
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
          <img className="brand-mark" src="/syvrio.svg" alt="Syvrio" width={30} height={30} />
          <button
            type="button"
            className="icon-button sidebar-close"
            onClick={onClose}
            aria-label="Close sessions"
            title="Hide sidebar (Ctrl+B)"
          >
            <PanelIcon size={20} />
          </button>
        </div>

        <button type="button" className="new-chat" onClick={onNew}>
          <PlusIcon size={16} />
          <span className="new-chat-label">New chat</span>
          <span className="new-chat-hint" aria-hidden="true">/new</span>
        </button>

        <div className="sidebar-section">Chats</div>
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
                title={`${session.title} · ${session.messages} messages`}
              >
                <span className="session-title">{session.title}</span>
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

        <div className="sidebar-foot">
          <span className="foot-avatar" aria-hidden="true">S</span>
          <span className="foot-model" title={model}>{model}</span>
          <span
            className={`foot-dot ${online ? 'online' : 'offline'}`}
            aria-hidden="true"
          />
        </div>
      </aside>
    </>
  );
}
