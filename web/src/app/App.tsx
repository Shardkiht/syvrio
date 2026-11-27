import { useCallback, useEffect, useRef, useState } from 'react';
import { CommandPalette, CommandResultDialog, ConfirmDialog, SessionCommandDialog } from '../features/commands/CommandUI';
import { Composer, type ComposerDraft } from '../features/chat/Composer';
import type { MeterReading } from '../features/chat/ContextMeter';
import { CommandsIcon, FileIcon, InboxIcon, PanelIcon, SettingsIcon } from '../shared/Icons';
import { RecoveredEdit } from '../features/chat/MessageActions';
import { MessageList } from '../features/chat/MessageList';
import { PendingReview } from '../features/commands/PendingReview';
import { SettingsPanel } from '../features/settings/SettingsPanel';
import { SurfacePresence } from '../shared/SurfacePresence';
import { Sidebar } from '../features/sessions/Sidebar';
import { SessionTitle, useSessionTitleEdits } from '../features/sessions/SessionTitle';
import { ArchivePanel } from '../features/sessions/ArchivePanel';
import { DreamPanel } from '../features/commands/DreamPanel';
import { TopicBrowser } from '../features/commands/TopicBrowser';
import { api } from '../api/client';
import { panelFor } from '../features/commands/commands';
import { useChat } from '../features/chat/useChat';
import { isComposing, matchShortcut } from '../shared/shortcuts';
import { applyTheme, readTheme, watchSystemTheme, type ThemeMode } from '../shared/theme';

async function readTopicPanel(line: string, session: string | null) {
  const response = await api.runCommand({ line, session_id: session, confirmed: false });
  const panel = panelFor(response);
  if (panel && 'kind' in panel && panel.kind === 'topics') return panel;
  throw new Error(response.message || 'Could not read topics.');
}

export default function App() {
  const picker = useRef<HTMLInputElement>(null);
  const drafts = useRef(new Map<string | null, ComposerDraft>());
  const meterReading = useRef<MeterReading | null>(null);
  const assignDraft = useCallback((blank: string, id: string) => {
    const draft = drafts.current.get(blank);
    if (draft) { drafts.current.set(id, draft); drafts.current.delete(blank); }
  }, []);
  const openFiles = useCallback(() => picker.current?.click(), []);
  const {
    chat: boundChat, preview, loadingHistory, sendAfterSelection, composerDraftId,
    selectionVersion, sendRevision,
    checkingSelection,
    deliveries,
    contextUsage,
    compaction,
    sessions,
    replyingSessionIds,
    health,
    online,
    sidebarOpen,
    setSidebarOpen,
    toggleSidebar,
    send,
    insert,
    queued,
    queuePaused,
    removeQueued,
    undoDraft,
    undoing,
    actOnLastMessage,
    failedEdit,
    discardFailedEdit,
    reviseFailedEdit,
    openMessageEditor,
    acknowledgeDraft,
    stop,
    newSession,
    selectSession,
    catalog,
    dialog, openDialog, closeDialog, confirmCommand,
    requestDeleteSession,
    archiveSession,
    refreshSessions,
    renameSession,
    confirmDeleteSession,
    refreshHealth,
    reviewMemory,
    runCommand,
    handleEscape,
    focusSignal,
    focusComposer,
    toggle,
    dismissError,
  } = useChat(openFiles, assignDraft);

  const chat = preview ?? boundChat;
  const model = health ? `${health.provider}/${health.model}` : 'Loading...';
  const empty = chat.messages.length === 0 && !loadingHistory;
  const currentSession = sessions.find(session => session.id === chat.sessionId);
  const title = currentSession?.title || 'New chat';
  const titleEditor = useSessionTitleEdits(renameSession);

  const [theme, setTheme] = useState<ThemeMode>(readTheme);
  const themeBooted = useRef(false);
  useEffect(() => {
    // 首次加载只恢复已保存的选择，用户主动选择后才写入。
    // 未设置主题的用户不会多出主题键。
    applyTheme(theme, themeBooted.current);
    themeBooted.current = true;
    // 自动模式下，系统主题变更应即时更新界面，无需重载。
    if (theme !== 'auto') return;
    return watchSystemTheme(() => applyTheme('auto', false));
  }, [theme]);

  // 在 iOS Safari 中，软键盘覆盖布局视口，仅用 vh 或 dvh 会挡住输入框。
  // 跟踪可视视口并将可用高度传给 CSS，同时兼容 Android 的 resizes-content 行为。
  useEffect(() => {
    const viewport = window.visualViewport;
    if (!viewport) return;
    const root = document.documentElement;
    const apply = () => {
      // 双指缩放也会缩小可视视口，此时保留原布局，让用户能平移查看放大后的内容。
      if (viewport.scale !== 1) return;
      root.style.setProperty('--app-height', `${Math.round(viewport.height)}px`);
    };
    apply();
    viewport.addEventListener('resize', apply);
    return () => {
      viewport.removeEventListener('resize', apply);
      root.style.removeProperty('--app-height');
    };
  }, []);

  const modalOpen = dialog !== null;
  const panel = dialog?.kind === 'panel' ? dialog.panel : null;
  const deleteTarget = dialog?.kind === 'delete' ? dialog.target : null;

  // 只注册一个全局监听器，快捷键绑定集中在 state/shortcuts.ts。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || isComposing(event)) return;
      const action = matchShortcut(event);
      if (!action) return;
      if (action === 'escape') {
        if (handleEscape()) {
          event.preventDefault();
        }
        return;
      }
      if (modalOpen) return;
      event.preventDefault();
      if (action === 'palette') {
        openDialog({ kind: 'palette' });
      } else if (action === 'sidebar') {
        toggleSidebar();
      } else if (action === 'model') {
        void runCommand('/model');
      } else if (action === 'settings') {
        openDialog({ kind: 'settings', section: 'model' });
      } else if (action === 'stop' && chat.busy) {
        void stop();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [chat.busy, handleEscape, modalOpen, openDialog, runCommand, stop, toggleSidebar]);

  return (
    <>
      <div ref={(node) => { node?.toggleAttribute('inert', modalOpen); }} className={`app${sidebarOpen ? '' : ' sidebar-collapsed'}`}>
        <Sidebar
          sessions={sessions}
          previewTitle={session => {
            const edit = titleEditor.edits[session.id];
            return (!edit?.error && edit?.value.trim()) || session.title;
          }}
          replyingSessionIds={replyingSessionIds}
          currentId={chat.sessionId}
          open={sidebarOpen}
          onNew={() => void newSession()}
          onSelect={(id) => void selectSession(id)}
          onArchive={(session, trigger) => openDialog({ kind: 'archive-confirm', session }, trigger)}
          onOpenArchive={() => openDialog({ kind: 'archive' })}
          onClose={() => setSidebarOpen(false)}
        />
        <main className={`main${empty ? ' is-empty' : ''}`}>
          <header className="topbar">
            <button
              type="button"
              className="icon-button sessions-toggle"
              onClick={() => setSidebarOpen(true)}
              aria-label="Open sessions"
              aria-controls="session-sidebar"
              aria-expanded={sidebarOpen}
              disabled={sidebarOpen}
              title="Show sidebar (Ctrl+B)"
            >
              <PanelIcon size={20} />
            </button>
            <div className="topbar-identity">
              {currentSession
                ? <SessionTitle key={currentSession.id} session={currentSession} editor={titleEditor} />
                : <div className="topbar-title">{title}</div>}
              <div className="topbar-model">
                <div
                  className={`connection ${online ? 'online' : 'offline'}`}
                  data-testid="connection"
                  title={online ? 'Connected' : 'Offline'}
                >
                  <span className="dot" />
                  <span className="connection-label">{online ? 'Connected' : 'Offline'}</span>
                </div>
                <button
                  type="button"
                  className="model-info"
                  data-testid="model-info"
                  aria-label={`Open model choices: ${model}`}
                  title={`${model} · Open model choices (Alt+M)`}
                  onClick={() => void runCommand('/model')}
                >
                  <span>{health?.model || model}</span>
                  <svg className="model-chevron" width="12" height="8" viewBox="0 0 12 8"
                    fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                    strokeLinejoin="round" aria-hidden="true" focusable="false">
                    <path d="m2 2 4 4 4-4" />
                  </svg>
                </button>
              </div>
            </div>
            <div className="topbar-actions">
              <button type="button" className="icon-button" aria-haspopup="dialog" aria-label="Browse memories"
                title="Browse memories" onClick={() => void runCommand('/topics')}><FileIcon size={22} /></button>
              <button
                type="button"
                className="icon-button commands-toggle"
                onClick={() => openDialog({ kind: 'palette' })}
                aria-label="Open commands"
                title="Commands (Alt+P)"
                data-testid="commands-toggle"
              >
                <CommandsIcon size={24} />
              </button>
              <button
                type="button"
                className="icon-button commands-toggle"
                onClick={() => openDialog({ kind: 'settings', section: 'model' })}
                aria-label="Open settings"
                title="Settings (Alt+S)"
                data-testid="settings-toggle"
              >
                <SettingsIcon size={24} />
              </button>
              <button type="button" className="icon-button pending-toggle" aria-haspopup="dialog"
                aria-label="Open pending review" title="Pending review" onClick={() => void runCommand('/pending')}>
                <InboxIcon size={22} /><span>Pending</span>
              </button>
            </div>
          </header>
          {chat.error && (
            <div className="error-banner" role="alert" data-testid="error-banner">
              <span>{chat.error}</span>
              <button type="button" onClick={dismissError}>Dismiss</button>
            </div>
          )}
          <div className="thread">
            <MessageList sendRevision={sendRevision} messages={chat.messages} sessionId={chat.sessionId} selectionVersion={selectionVersion} onToggle={toggle} loading={loadingHistory}
              undoing={checkingSelection || undoing || Boolean(failedEdit)} onMessageAction={checkingSelection ? undefined : actOnLastMessage} onEdit={openMessageEditor} />
            {!preview && deliveries.items.filter(item => item.error && item.sessionId === chat.sessionId).map(item => (
              <section key={item.id} className="delivery-notice" aria-label="Unconfirmed delivery">
                <strong>Delivery needs attention</strong>
                <p>{item.text}</p>
                {item.attachments.length > 0 && <small>{item.attachments.map(file => file.name).join(' · ')}</small>}
                {item.error !== chat.error && <div role="status">{item.error}</div>}
                <div className="confirm-actions">
                  <button type="button" onClick={() => deliveries.discard(item.id)}>Dismiss</button>
                  <button type="button" className="confirm-ok" onClick={() => void deliveries.retry(item)}>Retry delivery</button>
                </div>
              </section>
            ))}
            {!preview && failedEdit && <RecoveredEdit edit={failedEdit} busy={undoing}
              onChange={draft => reviseFailedEdit(failedEdit.sessionId, draft)}
              onRetry={() => void actOnLastMessage(failedEdit.sessionId, failedEdit.messageId, failedEdit.draft)}
              onDiscard={() => discardFailedEdit(failedEdit.sessionId)} />}
            <Composer
              key={selectionVersion}
              sessionId={composerDraftId}
              drafts={drafts.current}
              checkingSelection={checkingSelection}
              onSendAfterSelection={sendAfterSelection}
              pickerRef={picker}
              attachmentPolicy={health?.attachment_policy}
              openFiles={openFiles}
              busy={chat.busy}
              progress={chat.progress}
              newChat={chat.sessionId === null}
              contextUsage={preview ? undefined : contextUsage}
              meterReading={meterReading}
              compaction={preview ? null : compaction}
              onSend={(text, files) => void send(text, files)}
              onInsert={insert}
              queued={preview ? [] : queued}
              queuePaused={!preview && queuePaused}
              onRemoveQueued={removeQueued}
              undoDraft={preview ? undefined : undoDraft}
              onUndoDraftApplied={() => { if (chat.sessionId !== null) acknowledgeDraft(chat.sessionId); }}
              onStop={() => void stop()}
              catalog={catalog}
              focusSignal={focusSignal}
              hero={empty}
            />
          </div>
        </main>
      </div>

      <SurfacePresence modal>
        {dialog?.kind === 'archive-confirm' && (
          <ConfirmDialog
            key="archive-confirm"
            title="Archive conversation?"
            testId="archive-confirm-overlay"
            message={`Move “${dialog.session.title}” to Archive? You can restore it later.`}
            focusCancel
            confirmLabel="Archive"
            onCancel={closeDialog}
            onConfirm={() => {
              closeDialog();
              void archiveSession(dialog.session);
              focusComposer();
            }}
          />
        )}
        {dialog?.kind === 'palette' && (
          <CommandPalette
            key="palette"
            catalog={catalog}
            onRun={(line) => {
              closeDialog();
              void runCommand(line);
            }}
            onClose={closeDialog}
          />
        )}
        {dialog?.kind === 'confirm' && (
          <ConfirmDialog
            key="command-confirm"
            message={dialog.message}
            onConfirm={() => void confirmCommand()}
            onCancel={closeDialog}
          />
        )}
        {dialog?.kind === 'archive' && <ArchivePanel key="archive" initialSelected={dialog.selected} onClose={closeDialog}
          onRestore={refreshSessions} onDelete={requestDeleteSession} />}
        {deleteTarget && (
          <ConfirmDialog
            key="delete"
            title="Delete session"
            testId="delete-overlay"
            focusCancel
            confirmLabel="Delete permanently"
            confirmDisabled={!deleteTarget.preview}
            message={`Permanently delete "${deleteTarget.title}" and its uploaded files from Archive? This cannot be undone.`}
            onConfirm={() => { void confirmDeleteSession(); }}
            onCancel={closeDialog}
          >
            {deleteTarget.notice && <div role="status">{deleteTarget.notice}</div>}
            {!deleteTarget.preview && !deleteTarget.notice && <div role="status">Reading related pending items…</div>}
            {deleteTarget.preview && deleteTarget.preview.pending.length > 0 && (
              <div className="deletion-pending" tabIndex={0}>
                <strong>Pending items that will also be deleted ({deleteTarget.preview.pending.length})</strong>
                <ul>{deleteTarget.preview.pending.map(item => <li key={item.id}>{item.text}</li>)}</ul>
              </div>
            )}
          </ConfirmDialog>
        )}
        {dialog?.kind === 'settings' && (
          <SettingsPanel
            key="settings"
            health={health}
            initialSection={typeof dialog.section === 'object' ? dialog.section.section : dialog.section}
            initialNotice={typeof dialog.section === 'object' ? dialog.section.notice : undefined}
            theme={theme}
            onTheme={setTheme}
            onClose={closeDialog}
            onChanged={() => { void refreshHealth(); }}
          />
        )}
        {panel && ('rows' in panel ? (
          <PendingReview key={panel.kind} panel={panel}
            onRetry={() => void runCommand('/' + panel.kind)}
            onResolve={(action, id, text) => reviewMemory(panel.kind, action, id, text)}
            onClose={closeDialog} />
        ) : 'kind' in panel && panel.kind === 'topics' ? (
          <TopicBrowser key={panel.topic ?? 'topics'} initialTopics={panel.topics} initialTopic={panel.topic ?? panel.document?.topic}
            readTopics={async () => {
              const list = await readTopicPanel('/topics', null);
              if (!list.topics) throw new Error('The topic list is unavailable.');
              return list.topics;
            }}
            onClose={closeDialog} />
        ) : 'kind' in panel && panel.kind === 'dream' ? (
          <DreamPanel key="dream" panel={panel} onPick={runCommand} onClose={closeDialog} />
        ) : 'kind' in panel ? (
          <SessionCommandDialog key="sessions" sessions={sessions} currentId={chat.sessionId}
            onNew={() => { closeDialog(); void newSession(); }}
            onArchive={() => { openDialog({ kind: 'archive' }); }}
            onSelect={(id) => { closeDialog(); void selectSession(id); }} onClose={closeDialog} />
        ) : (
          <CommandResultDialog key={panel.title} panel={panel} title={title}
            onRun={runCommand} onClose={closeDialog} />
        ))}
      </SurfacePresence>
    </>
  );
}
