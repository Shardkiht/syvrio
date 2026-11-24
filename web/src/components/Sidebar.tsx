import type { SessionInfo } from '../types';
import { ResizableSidebar } from './ResizableSidebar';
import { ChatSearchPanel } from './ChatSearchPanel';
import { ArchiveIcon, PanelIcon, PlusIcon } from './Icons';

export function Sidebar({
  sessions,
  previewTitle,
  replyingSessionIds,
  currentId,
  open,
  onNew,
  onSelect,
  onArchive,
  onOpenArchive,
  onClose,
}: {
  sessions: SessionInfo[];
  previewTitle: (session: SessionInfo) => string;
  replyingSessionIds: ReadonlySet<string>;
  currentId: string | null;
  open: boolean;
  onNew: () => void;
  onSelect: (sessionId: string) => void;
  onArchive: (session: SessionInfo, trigger: HTMLButtonElement) => void;
  onOpenArchive: () => void;
  onClose: () => void;
}) {
  return (
    <>
      <div className="sidebar-scrim" data-open={open} aria-hidden="true"
        onClick={onClose} data-testid="sidebar-scrim" />
      <ResizableSidebar open={open}>
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

        <ChatSearchPanel onOpen={onSelect}>
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
                  aria-current={session.id === currentId ? 'page' : undefined}
                  title={`${session.title} · ${session.messages} messages`}
                >
                  {replyingSessionIds.has(session.id) && (
                    <span className="session-reply-dot" role="img" aria-label="Generating a reply" title="Generating a reply" />
                  )}
                  <span className="session-title">{previewTitle(session)}</span>
                </button>
                <button
                  type="button"
                  className="session-delete"
                  aria-haspopup="dialog"
                  onClick={event => onArchive(session, event.currentTarget)}
                  aria-label={`Archive ${session.title}`}
                  title="Move to Archive"
                  data-testid={`archive-${session.id}`}
                >
                  <ArchiveIcon size={15} />
                </button>
              </div>
            ))}
          </div>
        </ChatSearchPanel>
        <button type="button" className="archive-link" onClick={onOpenArchive}><ArchiveIcon size={16} />Archive</button>
      </ResizableSidebar>
    </>
  );
}
