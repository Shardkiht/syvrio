import { useCallback, useEffect, useRef, useState } from 'react';
import { CommandPalette, CommandPanelDialog, CommandResultDialog, ConfirmDialog, SessionCommandDialog } from './components/CommandUI';
import { Composer, type ComposerDraft } from './components/Composer';
import type { MeterReading } from './components/ContextMeter';
import { CommandsIcon, InboxIcon, PanelIcon, SettingsIcon } from './components/Icons';
import { RecoveredEdit } from './components/MessageActions';
import { MessageList } from './components/MessageList';
import { PendingReview } from './components/PendingReview';
import { SettingsPanel } from './components/SettingsPanel';
import { Sidebar } from './components/Sidebar';
import { ArchivePanel } from './components/ArchivePanel';
import { useChat } from './hooks/useChat';
import { isComposing, matchShortcut } from './state/shortcuts';
import { applyTheme, readTheme, watchSystemTheme, type ThemeMode } from './state/theme';
import type { SessionInfo } from './types';

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
    selectionVersion,
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
    paletteOpen,
    setPaletteOpen,
    panel,
    setPanel,
    confirm,
    setConfirm,
    confirmCommand,
    deleteTarget,
    setDeleteTarget,
    requestDeleteSession,
    archiveSession,
    refreshSessions,
    confirmDeleteSession,
    settingsOpen,
    setSettingsOpen,
    refreshHealth,
    runPanelCommand,
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
  const title = sessions.find((session) => session.id === chat.sessionId)?.title || 'New chat';

  const [theme, setTheme] = useState<ThemeMode>(readTheme);
  const themeBooted = useRef(false);
  useEffect(() => {
    // The first run only restores what was stored; persisting starts on a real
    // choice, so an untouched profile keeps no theme key at all.
    applyTheme(theme, themeBooted.current);
    themeBooted.current = true;
    // On "auto" a live OS switch must repaint the chrome without a reload.
    if (theme !== 'auto') return;
    return watchSystemTheme(() => applyTheme('auto', false));
  }, [theme]);

  // iOS Safari overlays the keyboard on the layout viewport, so `vh`/`dvh`
  // alone leaves the composer hidden behind it. Track the visual viewport and
  // hand the usable height to CSS; Android's resizes-content path agrees.
  useEffect(() => {
    const viewport = window.visualViewport;
    if (!viewport) return;
    const root = document.documentElement;
    const apply = () => {
      // Pinch-zoom shrinks the visual viewport too; leave the layout alone so
      // magnification still pans instead of squashing the whole app.
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

  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archivePrompt, setArchivePrompt] = useState<{ session: SessionInfo; trigger: HTMLButtonElement } | null>(null);
  const modalOpen = Boolean(archivePrompt || archiveOpen || paletteOpen || confirm || deleteTarget || settingsOpen || panel);
  const cancelArchive = useCallback(() => {
    setArchivePrompt(null);
    const trigger = archivePrompt?.trigger;
    requestAnimationFrame(() => {
      if (trigger?.isConnected && document.activeElement === document.body) trigger.focus({ preventScroll: true });
    });
  }, [archivePrompt]);

  // One global listener; every binding lives in state/shortcuts.ts.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || isComposing(event)) return;
      const action = matchShortcut(event);
      if (!action) return;
      if (archivePrompt) {
        if (action === 'escape') {
          event.preventDefault();
          cancelArchive();
        }
        return;
      }
      if (action === 'escape') {
        if (handleEscape()) {
          event.preventDefault();
          if (modalOpen) focusComposer();
        }
        return;
      }
      if (modalOpen) {
        if (action === 'settings' && panel && 'prefix' in panel && panel.prefix === '/model') {
          event.preventDefault();
          setPanel(null);
          setSettingsOpen('providers');
        }
        return;
      }
      event.preventDefault();
      if (action === 'palette') {
        setPanel(null);
        setPaletteOpen(true);
      } else if (action === 'sidebar') {
        toggleSidebar();
      } else if (action === 'model') {
        void runCommand('/model');
      } else if (action === 'settings') {
        setSettingsOpen('model');
      } else if (action === 'stop' && chat.busy) {
        void stop();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [archivePrompt, cancelArchive, chat.busy, focusComposer, handleEscape, modalOpen, panel, runCommand, setPaletteOpen, setPanel, setSettingsOpen, stop, toggleSidebar]);

  const closePalette = () => { setPaletteOpen(false); focusComposer(); };
  const closeConfirm = () => { setConfirm(null); focusComposer(); };
  const closeDelete = () => setDeleteTarget(null);
  const closeSettings = () => { setSettingsOpen(false); focusComposer(); };
  const closePanel = () => { setPanel(null); focusComposer(); };

  return (
    <>
      <div ref={(node) => { node?.toggleAttribute('inert', modalOpen); }} className={`app${sidebarOpen ? '' : ' sidebar-collapsed'}`}>
        <Sidebar
          sessions={sessions}
          replyingSessionIds={replyingSessionIds}
          currentId={chat.sessionId}
          open={sidebarOpen}
          onNew={() => void newSession()}
          onSelect={(id) => void selectSession(id)}
          onArchive={(session, trigger) => setArchivePrompt({ session, trigger })}
          onOpenArchive={() => setArchiveOpen(true)}
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
              <div className="topbar-title" title={title}>{title}</div>
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
              <button
                type="button"
                className="icon-button commands-toggle"
                onClick={() => setPaletteOpen(true)}
                aria-label="Open commands"
                title="Commands (Alt+P)"
                data-testid="commands-toggle"
              >
                <CommandsIcon size={24} />
              </button>
              <button
                type="button"
                className="icon-button commands-toggle"
                onClick={() => setSettingsOpen('model')}
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
            <MessageList messages={chat.messages} sessionId={chat.sessionId} selectionVersion={selectionVersion} onToggle={toggle} loading={loadingHistory}
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
              openFiles={openFiles}
              busy={chat.busy}
              progress={chat.progress}
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

      {archivePrompt && (
        <ConfirmDialog
          title="Archive conversation?"
          testId="archive-confirm-overlay"
          message={`Move “${archivePrompt.session.title}” to Archive? You can restore it later.`}
          focusCancel
          confirmLabel="Archive"
          onCancel={cancelArchive}
          onConfirm={() => {
            setArchivePrompt(null);
            void archiveSession(archivePrompt.session);
            focusComposer();
          }}
        />
      )}
      {paletteOpen && !archivePrompt && !confirm && !deleteTarget && !settingsOpen && (
        <CommandPalette
          catalog={catalog}
          onRun={(line) => {
            setPaletteOpen(false);
            void runCommand(line);
          }}
          onClose={closePalette}
        />
      )}
      {confirm && !archivePrompt && !deleteTarget && !settingsOpen && (
        <ConfirmDialog
          message={confirm.message}
          onConfirm={() => void confirmCommand()}
          onCancel={closeConfirm}
        />
      )}
      {archiveOpen && !archivePrompt && !deleteTarget && <ArchivePanel onClose={() => setArchiveOpen(false)}
        onRestore={refreshSessions} onDelete={requestDeleteSession} />}
      {deleteTarget && !archivePrompt && !settingsOpen && (
        <ConfirmDialog
          title="Delete session"
          testId="delete-overlay"
          focusCancel
          confirmLabel="Delete permanently"
          confirmDisabled={!deleteTarget.preview}
          message={`Permanently delete "${deleteTarget.title}" and its uploaded files from Archive? This cannot be undone.`}
          onConfirm={() => { void confirmDeleteSession(); }}
          onCancel={closeDelete}
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
      {settingsOpen && !archivePrompt && (
        <SettingsPanel
          health={health}
          initialSection={settingsOpen}
          theme={theme}
          onTheme={setTheme}
          onClose={closeSettings}
          onChanged={() => { void refreshHealth(); }}
        />
      )}
      {panel && !archivePrompt && !paletteOpen && !confirm && !deleteTarget && !settingsOpen && ('rows' in panel ? (
        <PendingReview key={panel.kind} panel={panel}
          onResolve={(action, id, text) => reviewMemory(panel.kind, action, id, text)}
          onClose={closePanel} />
      ) : 'options' in panel ? (
        <CommandPanelDialog panel={panel} onPick={(line) => void runPanelCommand(line)} onClose={closePanel} />
      ) : 'kind' in panel ? (
        <SessionCommandDialog sessions={sessions} currentId={chat.sessionId}
          onSelect={(id) => { closePanel(); void selectSession(id); }} onClose={closePanel} />
      ) : (
        <CommandResultDialog key={panel.title} panel={panel} title={title}
          onRun={(line) => void runPanelCommand(line)} onClose={closePanel} />
      ))}
    </>
  );
}
