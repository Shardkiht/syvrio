import { useCallback, useEffect, useRef, useState } from 'react';
import { api, NetworkError, streamChat } from '../api/client';
import {
  addInfo,
  initialChatState,
  loadHistory,
  reduceChat,
  startTurn,
  toggleSegment,
  type ChatState,
} from '../state/chat';
import { isCommandLine, panelFor, resultPanelFor, type CommandPanel, type PendingAction, type ReviewPanel } from '../state/commands';
import type { CommandInfo, CompactionPhase, ContextUsage, HealthResponse, SessionInfo, StreamEvent } from '../types';

const TERMINAL = new Set(['done', 'cancelled', 'failed']);
const FLUSH_MS = 16;

export function useChat() {
  const [chat, setChat] = useState<ChatState>(initialChatState);
  const chatRef = useRef<ChatState>(chat);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [usage, setUsage] = useState<ContextUsage | null>(null);
  const [compaction, setCompaction] = useState<CompactionPhase | null>(null);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [online, setOnline] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    // Desktop honours the last choice; the mobile drawer always starts closed.
    if (window.innerWidth < 768) return false;
    try {
      const stored = window.localStorage.getItem('syvrio.sidebar');
      if (stored === 'open') return true;
      if (stored === 'closed') return false;
    } catch {
      // Blocked storage: fall back to the width default.
    }
    return true;
  });
  useEffect(() => {
    if (window.innerWidth < 768) return;
    try {
      window.localStorage.setItem('syvrio.sidebar', sidebarOpen ? 'open' : 'closed');
    } catch {
      // Best effort only.
    }
  }, [sidebarOpen]);
  const [catalog, setCatalog] = useState<CommandInfo[]>([]);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [confirm, setConfirm] = useState<{ line: string; message: string } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<SessionInfo | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [panel, setPanel] = useState<CommandPanel | null>(null);
  const [focusSignal, setFocusSignal] = useState(0);
  const pendingHistoryRef = useRef(false);
  const navigation = useRef<Promise<void> | null>(null);
  const navigationVersion = useRef(0);
  const navigationHistory = useRef<Promise<void> | null>(null);
  const sessionsVersion = useRef(0);
  const newCommand = useRef<Promise<void> | null>(null);
  const commandSequence = useRef(0);

  // Crossing the mobile breakpoint must fold the drawer away; otherwise a
  // desktop window narrowed to phone width leaves the scrim covering the chat.
  useEffect(() => {
    let wide = window.innerWidth >= 768;
    const onResize = () => {
      const nextWide = window.innerWidth >= 768;
      if (nextWide !== wide) {
        wide = nextWide;
        setSidebarOpen(nextWide);
      }
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const update = useCallback((fn: (prev: ChatState) => ChatState) => {
    const next = fn(chatRef.current);
    chatRef.current = next;
    setChat(next);
  }, []);

  const pendingRef = useRef<StreamEvent[]>([]);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const turnRef = useRef<number | null>(null);
  const terminalRef = useRef(false);

  // Coalesce token deltas so one frame never triggers a re-render per token.
  const flush = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const events = pendingRef.current;
    pendingRef.current = [];
    if (events.length) update((prev) => events.reduce(reduceChat, prev));
  }, [update]);

  const refreshSessions = useCallback(async () => {
    const version = ++sessionsVersion.current;
    try {
      const result = await api.listSessions();
      if (version === sessionsVersion.current) setSessions(result.items);
    } catch {
      if (version === sessionsVersion.current) setOnline(false);
    }
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch {
      // Keep the last known label; a failed probe must not disable the composer.
    }
  }, []);

  useEffect(() => {
    if (!health || compaction === 'running' || compaction === 'failed') return;
    const controller = new AbortController();
    void api.contextUsage(chat.sessionId, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setUsage(value); })
      .catch(() => { if (!controller.signal.aborted) setUsage(null); });
    return () => controller.abort();
  }, [chat.sessionId, chat.busy, historyRevision, health, compaction]);

  useEffect(() => {
    if (compaction !== 'done' && compaction !== 'failed') return;
    const timer = setTimeout(() => setCompaction(null), 2200);
    return () => clearTimeout(timer);
  }, [compaction]);

  const reloadHistory = useCallback(async (sessionId: string | null) => {
    if (!sessionId) return;
    const version = navigationVersion.current;
    try {
      const history = await api.history(sessionId);
      if (version === navigationVersion.current && chatRef.current.sessionId === sessionId) {
        update((prev) => loadHistory(prev, history));
        setHistoryRevision((value) => value + 1);
      }
    } catch {
      // Keep the current view; a failed refresh must never disable the composer.
    }
  }, [update]);

  const runCommand = useCallback(async (
    line: string,
    confirmed = false,
    review?: { kind: ReviewPanel['kind']; text?: string },
  ) => {
    const requestLine = line.trim();
    if (!requestLine) return;
    while (navigation.current || newCommand.current) await (navigation.current ?? newCommand.current);
    const version = navigationVersion.current;
    const sequence = ++commandSequence.current;
    const sessionId = chatRef.current.sessionId;
    const isCompact = requestLine.split(/\s+/, 1)[0].toLowerCase() === '/compact';
    if (isCompact) setCompaction('running');
    try {
      const request = api.runCommand({
        line: requestLine,
        pending_text: review?.kind === 'pending' ? review.text : undefined,
        inference_text: review?.kind === 'inferences' ? review.text : undefined,
        session_id: sessionId,
        confirmed,
      });
      if (requestLine.split(/\s+/, 1)[0].toLowerCase() === '/new') {
        const settled = request.then(() => {}, () => {});
        newCommand.current = settled;
        void settled.finally(() => {
          if (newCommand.current === settled) newCommand.current = null;
        });
      }
      const result = await request;
      if (version !== navigationVersion.current) return;
      if (isCompact) {
        setCompaction(result.status !== 'ok' ? 'failed' : result.data?.compacted === true ? 'done' : null);
      }
      if (review) {
        const refreshed = panelFor(result);
        if (result.status !== 'ok' || !refreshed || !('rows' in refreshed) || refreshed.kind !== review.kind) {
          throw new Error(result.message || 'Unable to update the candidate.');
        }
        setPanel(refreshed);
        return result;
      }
      const canPresent = () => sequence === commandSequence.current && version === navigationVersion.current;
      if (result.status === 'confirm_required') {
        if (!canPresent()) return;
        setPanel(null);
        setConfirm({ line: requestLine, message: result.message });
        return;
      }
      if (canPresent()) setConfirm(null);

      if (result.status === 'client') {
        if (!canPresent()) return;
        if (result.intent === 'open_sessions') {
          setPanel({ kind: 'sessions' });
        } else if (result.intent === 'open_settings') {
          setPanel(null);
          setSettingsOpen(true);
        } else if (result.intent === 'leave') {
          // A Web /exit never stops the background server.
          update((prev) => ({ ...initialChatState, sessionId: prev.sessionId }));
          setPanel(resultPanelFor(result));
        } else {
          setPanel(resultPanelFor(result));
        }
        return;
      }
      if (result.status === 'ok' && result.intent === 'help') {
        if (!canPresent()) return;
        setPanel(null);
        setPaletteOpen(true);
        return;
      }
      const commandPanel = panelFor(result);
      if (commandPanel && canPresent()) setPanel(commandPanel);
      // /model, /think and /context can change the active model; keep the
      // header label in sync without a full page reload.
      if (result.data?.model_changed === true) void refreshHealth();
      if (result.refresh_sessions) void refreshSessions();
      if (result.status === 'ok' && result.intent === 'new_session') {
        // The file appears with the first message; until then this is just the
        // empty home view, so no session id is expected and there is no history
        // to reload (reading the old one would only flash before this reset).
        setCompaction(null);
        update(() => ({ ...initialChatState }));
      } else {
        if (result.refresh_history) {
          if (chatRef.current.busy) pendingHistoryRef.current = true;
          else await reloadHistory(result.session_id ?? chatRef.current.sessionId);
        }
      }
      if (!commandPanel && canPresent()) {
        const output = resultPanelFor(result);
        if (result.status === 'ok' && result.intent === 'rename' && requestLine.toLowerCase() === '/rename') {
          setPanel({ ...output, title: 'Rename session', message: '', rename: true });
        } else {
          setPanel(output);
        }
      }
    } catch (error) {
      if (review) throw error;
      if (isCompact && version === navigationVersion.current) setCompaction('failed');
      if (version === navigationVersion.current && sequence === commandSequence.current) {
        setPanel({
          title: 'Command failed',
          message: error instanceof Error ? error.message : 'unknown error',
        });
      }
    }
  }, [refreshHealth, refreshSessions, reloadHistory, update]);

  const handleEvent = useCallback((event: StreamEvent) => {
    if (event.type === 'compaction') {
      const phase = event.text;
      if (phase === 'running' || phase === 'done' || phase === 'failed') setCompaction(phase);
      return;
    }
    if (event.type === 'failed' || event.type === 'cancelled') {
      setCompaction((value) => value === 'running' ? 'failed' : value);
    }
    if (event.type === 'start' && event.turn_id != null) turnRef.current = event.turn_id;
    if (TERMINAL.has(event.type)) terminalRef.current = true;
    pendingRef.current.push(event);
    if (event.type === 'message_saved') {
      flush();
      void refreshSessions();
    } else if (timerRef.current === null) {
      timerRef.current = setTimeout(flush, FLUSH_MS);
    }
  }, [flush, refreshSessions]);

  const recoverReply = useCallback(async (
    turnId: number, sessionId: string | null, controller: AbortController,
  ) => {
    const { signal } = controller;
    const version = navigationVersion.current;
    const stillHere = () => !signal.aborted && version === navigationVersion.current;
    update((prev) => ({
      ...prev, busy: true, turnId, progress: '等待服务器完成回复', error: null,
    }));

    try {
      while (stillHere()) {
        const info = await api.health();
        if (!stillHere()) return;
        setHealth(info);
        setOnline(true);
        if (info.active_turn_id !== turnId) break;
        await new Promise<void>((resolve) => setTimeout(resolve, 1000));
      }
      if (!stillHere()) return;
      const listing = await api.listSessions();
      if (!stillHere()) return;
      setSessions(listing.items);
      const target = sessionId ?? listing.items.find((row) => row.current)?.id;
      if (!target) throw new Error('找不到这轮回复对应的会话');
      const history = await api.history(target);
      if (stillHere()) update((prev) => loadHistory(prev, history));
    } catch {
      if (stillHere()) {
        update((prev) => ({
          ...prev, busy: false, turnId: null, progress: '',
          error: '暂时无法读取回复，请重新打开会话查看结果',
        }));
      }
    }
  }, [update]);

  const send = useCallback(async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    // Every slash line goes to the command API, never to the LLM. Commands may
    // also run while a reply streams; the backend decides which ones are safe.
    if (isCommandLine(trimmed)) {
      await runCommand(trimmed);
      return;
    }
    const version = navigationVersion.current;
    while (navigation.current || newCommand.current) {
      await (navigation.current ?? newCommand.current);
    }
    const sessionId = chatRef.current.sessionId;
    const history = navigationHistory.current;
    if (history) await history;
    // The submit belongs to the view where the user pressed Send.
    if (version !== navigationVersion.current || navigation.current || newCommand.current
        || chatRef.current.sessionId !== sessionId) {
      update((prev) => ({ ...prev, error: '会话已切换，请在目标会话重新发送' }));
      return;
    }
    if (chatRef.current.busy) return;
    update((prev) => startTurn(prev, trimmed, sessionId));
    terminalRef.current = false;
    turnRef.current = null;
    const controller = new AbortController();
    controllerRef.current = controller;
    let failure: unknown = null;
    try {
      await streamChat({ message: trimmed, session_id: sessionId }, handleEvent, controller.signal);
    } catch (error) {
      failure = error;
    } finally {
      flush();
      const turnId = turnRef.current;
      if (controller.signal.aborted) {
        if (controllerRef.current === controller) controllerRef.current = null;
        return;
      }
      if (!terminalRef.current && turnId != null) {
        await recoverReply(turnId, chatRef.current.sessionId, controller);
      } else if (!terminalRef.current) {
        const message = failure instanceof Error
          ? failure.message
          : '连接已断开，请重新打开会话查看回复';
        update((prev) => reduceChat(prev, { seq: 0, type: 'failed', message }));
        // An HTTP error still means the server is reachable; only a transport
        // failure (or a stream that ended without a terminal event) is offline.
        if (!failure || failure instanceof NetworkError) setOnline(false);
      } else {
        setOnline(true);
      }
      if (controllerRef.current === controller) controllerRef.current = null;
      void refreshSessions();
      if (pendingHistoryRef.current) {
        pendingHistoryRef.current = false;
        await reloadHistory(chatRef.current.sessionId);
      }
    }
  }, [flush, handleEvent, recoverReply, refreshSessions, reloadHistory, runCommand, update]);

  const stop = useCallback(async () => {
    const turnId = turnRef.current ?? chatRef.current.turnId;
    if (turnId == null) return;
    try {
      await api.cancelTurn(turnId);
    } catch {
      update((prev) => ({ ...prev, error: '停止请求失败，回复仍可能在生成' }));
    }
  }, [update]);

  const navigate = useCallback((sessionId: string | null) => {
    if (chatRef.current.busy) {
      update((prev) => ({ ...prev, error: '请先停止当前回复，或等回复结束后再切换会话' }));
      return;
    }
    const version = ++navigationVersion.current;
    setPanel(null);
    setCompaction(null);
    navigationHistory.current = null;
    const previous = navigation.current ?? newCommand.current;
    const next = (async () => {
      await previous;
      if (version !== navigationVersion.current) return;
      try {
        if (sessionId === null) await api.createSession();
        else await api.switchSession(sessionId);
        // Apply the committed selection before fetching history. If the read
        // fails, the composer still targets the session the server selected.
        update(() => ({ ...initialChatState, sessionId }));
        if (sessionId !== null && version === navigationVersion.current) {
          const loading = api.history(sessionId).then((history) => {
            if (version === navigationVersion.current && !chatRef.current.busy
                && chatRef.current.sessionId === sessionId && chatRef.current.messages.length === 0) {
              update((prev) => loadHistory(prev, history));
            }
          }).catch((error: unknown) => {
            if (version === navigationVersion.current && chatRef.current.sessionId === sessionId) {
              update((prev) => ({
                ...prev,
                error: error instanceof Error ? error.message : '会话历史加载失败',
              }));
            }
          }).finally(() => {
            if (navigationHistory.current === loading) navigationHistory.current = null;
          });
          navigationHistory.current = loading;
        }
        if (version === navigationVersion.current && window.innerWidth < 768) setSidebarOpen(false);
      } catch (error) {
        if (version === navigationVersion.current) {
          update((prev) => ({
            ...prev,
            error: error instanceof Error ? error.message : '会话切换失败',
          }));
        }
      } finally {
        void refreshSessions();
      }
    })();
    navigation.current = next;
    void next.finally(() => {
      if (navigation.current === next) navigation.current = null;
    });
    return next;
  }, [refreshSessions, update]);

  const newSession = useCallback(() => navigate(null), [navigate]);
  const selectSession = useCallback((sessionId: string) => navigate(sessionId), [navigate]);

  const requestDeleteSession = useCallback((session: SessionInfo) => {
    if (chatRef.current.busy) {
      update((prev) => ({ ...prev, error: '请先停止当前回复，再删除会话' }));
      return;
    }
    setDeleteTarget(session);
  }, [update]);

  const confirmDeleteSession = useCallback(async () => {
    const target = deleteTarget;
    if (!target) return;
    setDeleteTarget(null);
    try {
      // The Application guard (same as TUI) refuses to delete the live
      // session. Detach to the empty view first (no file is created), then
      // remove the old JSONL file.
      while (navigation.current) await navigation.current;
      if (target.id === chatRef.current.sessionId) {
        await navigate(null);
        if (chatRef.current.sessionId === target.id) return;
      }
      await api.deleteSession(target.id);
      await refreshSessions();
    } catch (error) {
      update((prev) => addInfo(
        prev,
        `Delete failed: ${error instanceof Error ? error.message : 'unknown error'}`,
      ));
    } finally {
      setFocusSignal((value) => value + 1);
    }
  }, [deleteTarget, navigate, refreshSessions, update]);

  useEffect(() => {
    let alive = true;
    const recovery = new AbortController();
    const version = navigationVersion.current;
    const sessionListVersion = sessionsVersion.current;
    void (async () => {
      try {
        const [info, listing, commands] = await Promise.all([
          api.health(),
          api.listSessions(),
          api.commands().catch(() => ({ items: [] as CommandInfo[] })),
        ]);
        if (!alive) return;
        setHealth(info);
        if (version === navigationVersion.current && sessionListVersion === sessionsVersion.current) {
          setSessions(listing.items);
        }
        setCatalog(commands.items);
        setOnline(true);
        if (!info.static_ready) {
          update((prev) => ({ ...prev, error: 'Web 静态资源未构建，请运行 scripts/build_web.py' }));
        }
        const current = listing.items.find((item) => item.current);
        if (version === navigationVersion.current && !chatRef.current.busy
            && info.active_turn_id != null) {
          if (current) {
            update((prev) => ({ ...prev, sessionId: current.id }));
          }
          await recoverReply(info.active_turn_id, current?.id ?? null, recovery);
        } else if (current && version === navigationVersion.current && !chatRef.current.busy) {
          const history = await api.history(current.id);
          if (alive && version === navigationVersion.current && !chatRef.current.busy) {
            update((prev) => loadHistory(prev, history));
          }
        }
      } catch {
        if (alive) setOnline(false);
      }
    })();
    const goOnline = () => setOnline(true);
    const goOffline = () => setOnline(false);
    window.addEventListener('online', goOnline);
    window.addEventListener('offline', goOffline);
    return () => {
      alive = false;
      recovery.abort();
      window.removeEventListener('online', goOnline);
      window.removeEventListener('offline', goOffline);
      controllerRef.current?.abort();
    };
  }, [recoverReply, update]);

  const toggleSidebar = useCallback(() => setSidebarOpen((open) => !open), []);
  const focusComposer = useCallback(() => setFocusSignal((value) => value + 1), []);

  const handleEscape = useCallback(() => {
    if (settingsOpen) { setSettingsOpen(false); return true; }
    if (deleteTarget) { setDeleteTarget(null); return true; }
    if (confirm) { setConfirm(null); return true; }
    if (paletteOpen) { setPaletteOpen(false); return true; }
    if (panel) { setPanel(null); return true; }
    if (chatRef.current.busy) { void stop(); return true; }
    return false;
  }, [confirm, deleteTarget, paletteOpen, panel, settingsOpen, stop]);

  const confirmCommand = useCallback(async () => {
    if (!confirm) return;
    const { line } = confirm;
    setConfirm(null);
    await runCommand(line, true);
  }, [confirm, runCommand]);

  const reviewMemory = useCallback(async (
    kind: ReviewPanel['kind'], action: PendingAction, id: string, text?: string,
  ) => {
    const inferenceAction = { approve: 'confirm', reject: 'reject', edit: 'correct' };
    const verb = kind === 'pending' ? action : inferenceAction[action];
    const result = await runCommand(`/${kind} ${verb} ${id}`, true, { kind, text });
    if (!result) throw new Error('The current session changed. Reopen the review.');
  }, [runCommand]);

  const runPanelCommand = useCallback(async (line: string) => {
    setPanel(null);
    await runCommand(line);
  }, [runCommand]);

  return {
    chat,
    contextUsage: usage?.session_id === chat.sessionId ? usage : null,
    compaction,
    sessions,
    health,
    online,
    sidebarOpen,
    setSidebarOpen,
    toggleSidebar,
    refreshSessions,
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
    refreshHealth,
    runPanelCommand,
    reviewMemory,
    runCommand,
    handleEscape,
    focusSignal,
    focusComposer,
    toggle: (segmentId: string) => update((prev) => toggleSegment(prev, segmentId)),
    dismissError: () => update((prev) => ({ ...prev, error: null })),
  };
}
