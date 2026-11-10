import { useEffect } from 'react';
import { CommandPalette, CommandPanelDialog, ConfirmDialog } from './components/CommandUI';
import { Composer } from './components/Composer';
import { MessageList } from './components/MessageList';
import { SettingsPanel } from './components/SettingsPanel';
import { Sidebar } from './components/Sidebar';
import { useChat } from './hooks/useChat';
import { isComposing, matchShortcut } from './state/shortcuts';

export default function App() {
  const {
    chat,
    sessions,
    health,
    online,
    sidebarOpen,
    setSidebarOpen,
    toggleSidebar,
    send,
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
    confirmDeleteSession,
    settingsOpen,
    setSettingsOpen,
    notice,
    refreshHealth,
    runPanelCommand,
    runCommand,
    handleEscape,
    focusSignal,
    focusComposer,
    toggle,
    dismissError,
  } = useChat();

  const model = health ? `${health.provider}/${health.model}` : 'Loading...';

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

  // One global listener; every binding lives in state/shortcuts.ts.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isComposing(event)) return;
      const action = matchShortcut(event);
      if (!action) return;
      if (action === 'escape') {
        if (handleEscape()) event.preventDefault();
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
        setSettingsOpen(true);
      } else if (action === 'stop' && chat.busy) {
        void stop();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [chat.busy, handleEscape, runCommand, setPaletteOpen, setPanel, setSettingsOpen, stop, toggleSidebar]);

  const closePalette = () => { setPaletteOpen(false); focusComposer(); };
  const closeConfirm = () => { setConfirm(null); focusComposer(); };
  const closeDelete = () => { setDeleteTarget(null); focusComposer(); };
  const closeSettings = () => { setSettingsOpen(false); focusComposer(); };
  const closePanel = () => { setPanel(null); focusComposer(); };

  return (
    <div className="app">
      <Sidebar
        sessions={sessions}
        currentId={chat.sessionId}
        open={sidebarOpen}
        model={model}
        onNew={() => void newSession()}
        onSelect={(id) => void selectSession(id)}
        onDelete={requestDeleteSession}
        onClose={() => setSidebarOpen(false)}
      />
      <main className="main">
        <header className="topbar">
          <button
            type="button"
            className="sessions-toggle"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open sessions"
          >
            Sessions
          </button>
          <button
            type="button"
            className="commands-toggle"
            onClick={() => setPaletteOpen(true)}
            aria-label="Open commands"
            data-testid="commands-toggle"
          >
            Commands
          </button>
          <button
            type="button"
            className="commands-toggle"
            onClick={() => setSettingsOpen(true)}
            aria-label="Open settings"
            data-testid="settings-toggle"
          >
            Settings
          </button>
          <button
            type="button"
            className="model-info"
            data-testid="model-info"
            title="Open model choices (/model, Alt+M)"
            onClick={() => void runCommand('/model')}
          >
            {model}
          </button>
          <div
            className={`connection ${online ? 'online' : 'offline'}`}
            data-testid="connection"
            title={online ? 'Connected' : 'Offline'}
          >
            <span className="dot" />
            <span className="connection-label">{online ? 'Connected' : 'Offline'}</span>
          </div>
        </header>
        {chat.error && (
          <div className="error-banner" role="alert" data-testid="error-banner">
            <span>{chat.error}</span>
            <button type="button" onClick={dismissError}>Dismiss</button>
          </div>
        )}
        <MessageList messages={chat.messages} onToggle={toggle} />
        <Composer
          busy={chat.busy}
          progress={chat.progress}
          onSend={(text) => void send(text)}
          onStop={() => void stop()}
          catalog={catalog}
          focusSignal={focusSignal}
        />
      </main>

      {paletteOpen && (
        <CommandPalette
          catalog={catalog}
          onRun={(line) => {
            setPaletteOpen(false);
            void runCommand(line);
            focusComposer();
          }}
          onClose={closePalette}
        />
      )}
      {confirm && (
        <ConfirmDialog
          message={confirm.message}
          onConfirm={() => { void confirmCommand(); focusComposer(); }}
          onCancel={closeConfirm}
        />
      )}
      {deleteTarget && (
        <ConfirmDialog
          title="Delete session"
          testId="delete-overlay"
          confirmLabel="Delete permanently"
          message={
            `Delete session "${deleteTarget.title}" permanently? This cannot be undone.`
            + (deleteTarget.current
              ? '\nThe current session will be replaced by a new empty chat first.'
              : '')
          }
          onConfirm={() => { void confirmDeleteSession(); focusComposer(); }}
          onCancel={closeDelete}
        />
      )}
      {settingsOpen && (
        <SettingsPanel
          health={health}
          onClose={closeSettings}
          onChanged={() => { void refreshHealth(); }}
          onNotice={notice}
        />
      )}
      {panel && (
        <CommandPanelDialog
          panel={panel}
          onPick={(line) => { void runPanelCommand(line); focusComposer(); }}
          onClose={closePanel}
        />
      )}
    </div>
  );
}
