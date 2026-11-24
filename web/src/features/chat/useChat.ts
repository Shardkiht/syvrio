import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, NetworkError, newRequestId, removeQueuedTurn, submitChat, type TurnReceipt } from '../../api/client';
import { useDialogNavigation, type DeleteTarget } from '../commands/useDialogNavigation';
import { useDeliveries } from './useDeliveries';
import { useSessionActivity } from '../sessions/useSessionActivity';
import { useSessionNavigation } from '../sessions/useSessionNavigation';
import { useSelectionHistory } from '../sessions/useSelectionHistory';
import { useTurnStream } from './useTurnStream';
import {
  addInfo,
  initialChatState,
  toggleSegment,
  type ChatState,
} from './chat';
import { isCommandLine, panelFor, resultPanelFor, readPanelTitle, panelMatches, type PendingAction, type ReviewPanel } from '../commands/commands';
import type { CommandInfo, CompactionPhase, ContextUsage, HealthResponse, SessionInfo } from '../../types';

import { readEditFiles, saveEditFiles } from './editFiles';
import { forgetEdit, loadEdits, missingFiles, persistEdit, tabOnly, type EditedDraft, type FailedEdit } from './editRecovery';

type MessageAction = { sessionId: string; messageId: string };

export function useChat(openFiles?: () => void, assignDraft?: (blankId: string, sessionId: string) => void) {
  const [chat, setChat] = useState<ChatState>(initialChatState);
  const chatRef = useRef<ChatState>(chat);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [usage, setUsage] = useState<{ sessionId: string | null; value: ContextUsage | null }>();
  const [compaction, setCompaction] = useState<CompactionPhase | null>(null);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [online, setOnline] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    // 桌面端沿用上次选择，移动端抽屉初始总是关闭。
    if (window.innerWidth < 768) return false;
    try {
      const stored = window.localStorage.getItem('syvrio.sidebar');
      if (stored === 'open') return true;
      if (stored === 'closed') return false;
    } catch {
      // 本地存储不可用时，按默认宽度处理。
    }
    return true;
  });
  useEffect(() => {
    if (window.innerWidth < 768) return;
    try {
      window.localStorage.setItem('syvrio.sidebar', sidebarOpen ? 'open' : 'closed');
    } catch {
      // 存储不可用时仍保留当前界面状态。
    }
  }, [sidebarOpen]);
  const [catalog, setCatalog] = useState<CommandInfo[]>([]);
  const { dialog, open, close, setPanel, setDialog } = useDialogNavigation();
  const confirm = dialog?.kind === 'confirm' ? dialog : null;
  const deleteTarget = dialog?.kind === 'delete' ? dialog.target : null;
  const [sendRevision, setSendRevision] = useState(0);
  const [focusSignal, setFocusSignal] = useState(0);
  const [queued, setQueued] = useState<TurnReceipt[]>([]);
  const queueRef = useRef<TurnReceipt[]>([]);
  const [queuePaused, setQueuePaused] = useState(false);
  const [undoDrafts, setUndoDrafts] = useState<Record<string, string>>({});
  const changeQueue = useCallback((items: TurnReceipt[]) => {
    queueRef.current = items;
    setQueued(items);
  }, []);
  const acknowledgeDraft = useCallback((sessionId: string) => {
    setUndoDrafts((drafts) => {
      const next = { ...drafts };
      delete next[sessionId];
      return next;
    });
  }, []);
  const [failedEdits, setFailedEdits] = useState(loadEdits);
  const failedEditsRef = useRef(failedEdits);
  const offlineRecoverySession = useRef<string | null>(null);
  const showFailedEdit = useCallback((edit: FailedEdit) => {
    failedEditsRef.current = { ...failedEditsRef.current, [edit.sessionId]: edit };
    setFailedEdits(failedEditsRef.current);
  }, []);
  const failedEdit = chat.sessionId ? failedEdits[chat.sessionId] : undefined;
  const savedDraft = failedEdit?.draft;
  useEffect(() => {
    if (!failedEdit || !savedDraft?.editId || savedDraft.files || !savedDraft.filesStored) return;
    let current = true;
    void readEditFiles(savedDraft.editId, savedDraft.attachments).then(files => {
      if (current && failedEditsRef.current[failedEdit.sessionId] === failedEdit) {
        showFailedEdit({ ...failedEdit, draft: { ...savedDraft, files } });
      }
    }).catch(() => {
      if (current && failedEditsRef.current[failedEdit.sessionId] === failedEdit) {
        showFailedEdit({ ...failedEdit, draft: { ...savedDraft, filesStored: false }, warning: missingFiles });
      }
    });
    return () => { current = false; };
  }, [failedEdit, savedDraft, showFailedEdit]);
  const syncRef = useRef<() => Promise<void>>(async () => {});
  const revision = useRef<{ session: string | null; value: number } | null>(null);
  const navigationVersion = useRef(0);
  const navigationHistory = useRef<Promise<void> | null>(null);
  const sessionsVersion = useRef(0);
  const commandSequence = useRef(0);
  const commandWaitingForSwitch = useRef<number | null>(null);
  const reviewCache = useRef<Partial<Record<ReviewPanel['kind'], Pick<ReviewPanel, 'rows' | 'count'>>>>({});

  // 跨入移动端宽度时收起抽屉，避免桌面窗口缩窄后遮罩仍覆盖对话。
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
    if (next.sessionId !== chatRef.current.sessionId) {
      try {
        if (next.sessionId) window.sessionStorage.setItem('syvrio.session', next.sessionId);
        else window.sessionStorage.removeItem('syvrio.session');
      } catch { /* 本地存储不可用时仍可浏览。 */ }
    }
    chatRef.current = next;
    setChat(next);
  }, []);

  const refreshSessions = useCallback(async () => {
    const version = ++sessionsVersion.current;
    try {
      const result = await api.listSessions();
      if (version === sessionsVersion.current) setSessions(result.items);
    } catch {
      if (version === sessionsVersion.current) setOnline(false);
    }
  }, []);

  const { replyingSessionIds, readQueue } = useSessionActivity(sessions, refreshSessions);

  const renameSession = useCallback(async (id: string, title: string) => {
    await api.renameSession(id, title);
    ++sessionsVersion.current;
    setSessions(rows => rows.map(row => row.id === id ? { ...row, title, title_pending: false } : row));
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch {
      // 探测失败时保留上次的标签，输入框仍可用。
    }
  }, []);

  useEffect(() => {
    if (!health || compaction === 'running' || compaction === 'failed') return;
    const controller = new AbortController();
    void api.contextUsage(chat.sessionId, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setUsage({ sessionId: chat.sessionId, value: value.session_id === chat.sessionId ? value : null });
        }
      })
      .catch(() => { if (!controller.signal.aborted) setUsage({ sessionId: chat.sessionId, value: null }); });
    return () => controller.abort();
  }, [chat.sessionId, chat.busy, historyRevision, health, compaction]);

  useEffect(() => {
    if (compaction !== 'done' && compaction !== 'failed') return;
    const timer = setTimeout(() => setCompaction(null), 2200);
    return () => clearTimeout(timer);
  }, [compaction]);

  const onHistoryRefresh = useCallback(() => setHistoryRevision(value => value + 1), []);
  const { preview, startPreview, finishPreview, togglePreview, switchHistory, remember, forget, loadSelectionHistory,
    reloadHistory, committedKey, blankDraftId, loadingHistory } =
    useSelectionHistory(chatRef, navigationVersion, navigationHistory, update, onHistoryRefresh);

  const { stream, detach, observe } = useTurnStream({
    chat: chatRef, selectionHistory: navigationHistory, syncQueue: syncRef, update,
    refreshSessions, reloadHistory, setCompaction, setOnline,
  });

  const onRequested = useCallback((id: string | null, waiting: boolean) => {
    startPreview(id, waiting);
    if (window.innerWidth < 768) setSidebarOpen(false);
  }, [startPreview]);

  const currentSession = useCallback(() => chatRef.current.sessionId, []);
  const deliveryFailed = useCallback((error: unknown) => {
    if (error instanceof NetworkError) setOnline(false);
    update(prev => ({ ...prev, error: error instanceof Error ? error.message : '发送失败' }));
  }, [update]);
  const onSelected = useCallback((sessionId: string | null, latest: boolean, confirmed?: ChatState) => {
    // 后端提交当前视图的新绑定后才解除原关联。
    ++navigationVersion.current;
    detach();
    navigationHistory.current = null;
    offlineRecoverySession.current = null;
    revision.current = null;
    changeQueue([]);
    setQueuePaused(false);
    const keepWaitingPanel = commandWaitingForSwitch.current === commandSequence.current;
    setDialog(current => {
      if (current?.kind === 'confirm') return null;
      if (current?.kind !== 'panel') return current;
      const p = current.panel;
      return keepWaitingPanel || 'rows' in p || ('kind' in p && p.kind === 'topics') ? current : null;
    });
    setCompaction(null);
    const prior = latest ? finishPreview(true) : undefined;
    const snapshot = confirmed ?? prior;
    update(() => snapshot?.sessionId === sessionId ? snapshot : { ...initialChatState, sessionId });
    if (sessionId !== null && latest && !confirmed) loadSelectionHistory(sessionId);
  }, [changeQueue, detach, finishPreview, loadSelectionHistory, update]);
  const onNavigationFailure = useCallback((error: unknown) => {
    finishPreview(false);
    const selected = chatRef.current;
    if (selected.sessionId && !selected.messages.length && !stream.current && !navigationHistory.current) {
      loadSelectionHistory(selected.sessionId);
    }
    update(prev => ({ ...prev, error: error instanceof Error ? error.message : '会话切换失败' }));
  }, [finishPreview, loadSelectionHistory, stream, update]);
  const onNavigationSettled = useCallback(() => {
    void refreshSessions();
    void syncRef.current().catch(() => undefined);
  }, [refreshSessions]);
  const { navigation, navigate, queueSubmission, submitWhenSelected, editing, beginEdit, undoing, checkingSelection, reconcileSelection } = useSessionNavigation({
    currentSession, onRequested, switchHistory, onSelected, onFailure: onNavigationFailure, onSettled: onNavigationSettled, onDeliveryError: deliveryFailed,
  });

  const openReview = useCallback(async (kind: ReviewPanel['kind']) => {
    const sequence = ++commandSequence.current;
    const opening: ReviewPanel = {
      kind, title: kind === 'pending' ? 'Pending review' : 'Inference review',
      rows: [], count: 0, ...reviewCache.current[kind], loading: true,
    };
    setPanel(opening);
    try {
      // 两份列表均属于 Vault，等待切换会话时也可读取。
      const result = await api.runCommand({ line: '/' + kind, session_id: null, confirmed: false });
      const next = panelFor(result);
      if (result.status !== 'ok' || !next || !('rows' in next) || next.kind !== kind) {
        throw new Error(result.message || 'Unable to load the list.');
      }
      if (sequence !== commandSequence.current) return;
      reviewCache.current[kind] = { rows: next.rows, count: next.count };
      setPanel(current => current === opening ? next : current);
    } catch (error) {
      if (sequence !== commandSequence.current) return;
      const loadError = error instanceof Error ? error.message : 'Unable to load the list.';
      setPanel(current => current === opening ? { ...opening, loading: false, loadError } : current);
    }
  }, []);

  const runCommand = useCallback(async (
    line: string,
    confirmed = false,
    review?: { kind: ReviewPanel['kind']; text?: string },
    messageAction?: MessageAction,
  ) => {
    const requestLine = line.trim();
    if (!requestLine) return;
    const bare = requestLine.toLowerCase();
    if (bare === '/pending' || bare === '/inferences') {
      return openReview(bare === '/pending' ? 'pending' : 'inferences');
    }
    // 原生文件选择器必须在键盘或指针事件触发时打开。
    const native = catalog.find((entry) => entry.support === 'client' && entry.intent === 'pick_attachments'
      && requestLine.toLowerCase() === '/' + entry.name);
    if (native && openFiles) {
      openFiles();
      return;
    }
    if (bare === '/new') {
      await navigate(null, requestLine);
      return;
    }
    const sequence = ++commandSequence.current;
    const settingsSections = { '/model': 'model', '/think': 'thinking', '/context': 'context' } as const;
    if (bare === '/model' || bare === '/think' || bare === '/context') {
      open({ kind: 'settings', section: settingsSections[bare] });
      return;
    }
    if (requestLine.toLowerCase() === '/topics') {
      // 立即打开浏览面板，由面板载入共享命令结果并处理重试。
      setPanel({ kind: 'topics' });
      return;
    }
    const readTitle = readPanelTitle(bare);
    const trigger = document.activeElement;
    const parent = dialog?.kind === 'panel' ? dialog.panel : undefined;
    const triggerLabel = trigger?.closest('[role="dialog"]') && trigger instanceof HTMLButtonElement
      ? trigger.getAttribute('aria-label') || trigger.textContent?.trim() : undefined;
    if (bare === '/sessions') {
      setPanel({ kind: 'sessions' });
    } else if (readTitle && !(dialog?.kind === 'panel' && panelMatches(dialog.panel, readTitle))) {
      setPanel(bare === '/dream status'
        ? { kind: 'dream', state: 'loading' }
        : { title: readTitle, message: 'Loading…' });
    } else if (!readTitle && !review && !messageAction && (confirmed || !parent)) {
      setPanel({ title: requestLine.split(/\s+/, 1)[0], message: 'Submitting…' });
    }
    const target = navigation.current;
    if (target && readTitle) commandWaitingForSwitch.current = sequence;
    const selected = target ? await target && (!navigation.current || navigation.current === target) : !checkingSelection;
    if (commandWaitingForSwitch.current === sequence) commandWaitingForSwitch.current = null;
    if (!selected) {
      if (!review && !messageAction && sequence === commandSequence.current) {
        setPanel({ title: readTitle ?? 'Command not sent', message: 'The conversation changed. Try again.', feedback: 'error',
          actions: readTitle ? [{ label: 'Retry', line: requestLine }] : undefined });
      }
      return;
    }
    if (sequence !== commandSequence.current) return;
    const version = navigationVersion.current;
    const sessionId = chatRef.current.sessionId;
    if (messageAction && sessionId !== messageAction.sessionId) return;
    const isCompact = bare === '/compact';
    const isUndo = bare === '/undo';
    if (isUndo && editing.current) return;
    const finishEdit = isUndo ? beginEdit() : undefined;
    if (isCompact) setCompaction('running');
    if (review) {
      setPanel(current => current && 'rows' in current && current.kind === review.kind && current.loading
        ? { ...current, loading: false } : current);
    }
    const canPresent = () => sequence === commandSequence.current && version === navigationVersion.current;
    try {
      const result = await api.runCommand({
        line: requestLine,
        expected_message_id: messageAction?.messageId,
        pending_text: review?.kind === 'pending' ? review.text : undefined,
        inference_text: review?.kind === 'inferences' ? review.text : undefined,
        session_id: sessionId,
        confirmed,
      });
      if (version !== navigationVersion.current) return;
      if (isCompact) {
        setCompaction(result.status !== 'ok' ? 'failed' : result.data?.compacted === true ? 'done' : null);
      }
      const commandPanel = panelFor(result);
      if (result.status === 'ok' && commandPanel && 'rows' in commandPanel && canPresent()) {
        reviewCache.current[commandPanel.kind] = { rows: commandPanel.rows, count: commandPanel.count };
      }
      if (review) {
        if (result.status !== 'ok' || !commandPanel || !('rows' in commandPanel) || commandPanel.kind !== review.kind) {
          throw new Error(result.message || 'Unable to update the candidate.');
        }
        if (canPresent()) setPanel(commandPanel);
        return result;
      }
      if (result.status === 'confirm_required' && !(commandPanel && 'rows' in commandPanel && commandPanel.edit)) {
        if (!canPresent()) return;
        open({ kind: 'confirm', line: requestLine, message: result.message, parent, triggerLabel });
        return;
      }

      if (result.status === 'client') {
        if (!canPresent()) return;
        if (result.intent === 'pick_attachments') {
          openFiles?.();
        } else if (result.intent === 'open_sessions') {
          setPanel({ kind: 'sessions' });
        } else if (result.intent === 'open_settings' || result.intent === 'open_update') {
          open({ kind: 'settings', section: result.intent === 'open_update' ? 'about' : 'providers' });
        } else if (result.intent === 'leave') {
          if (await navigate(null)) setPanel(resultPanelFor(result));
        } else {
          setPanel(resultPanelFor(result));
        }
        return;
      }
      if (result.status === 'ok' && result.intent === 'help') {
        if (!canPresent()) return;
        open({ kind: 'palette' });
        return;
      }
      if (result.status === 'ok' && result.intent === 'model' && Array.isArray(result.data?.options)) {
        if (canPresent()) {
          open({ kind: 'settings', section: { section: 'model', notice: String(result.data?.refresh_message || 'Model catalog checked.') } });
        }
        return;
      }
      if (commandPanel && canPresent()) setPanel(commandPanel);
      // /model、/think 和 /context 都可能改变当前模型，需同步刷新顶栏标签，无需重新加载页面。
      if (result.data?.model_changed === true) void refreshHealth();
      if (result.refresh_sessions) void refreshSessions();
      if (result.refresh_history && !chatRef.current.busy) {
        await reloadHistory(result.session_id ?? chatRef.current.sessionId);
      }
      if (isUndo && result.status === 'ok' && typeof result.data?.restored === 'string') {
        const target = result.session_id ?? chatRef.current.sessionId;
        if (target !== null) {
          const text = result.data.restored;
          setUndoDrafts((drafts) => ({ ...drafts, [target]: text }));
          setQueuePaused(true);
        }
        if (canPresent()) setPanel(null);
        setFocusSignal((value) => value + 1);
        return result;
      }
      if (!commandPanel && canPresent()) {
        const output = resultPanelFor(result);
        if (result.status === 'ok' && result.intent === 'rename' && requestLine.toLowerCase() === '/rename') {
          setPanel({ ...output, title: 'Rename session', message: '', rename: true });
        } else if (result.intent === 'rename' && result.status === 'error' && bare.startsWith('/rename ')) {
          setPanel({ ...output, title: 'Rename session', rename: true, draft: requestLine.slice(7).trim() });
        } else {
          setPanel(output);
        }
      }
    } catch (error) {
      if (review) throw error;
      if (isCompact && version === navigationVersion.current) setCompaction('failed');
      if (version === navigationVersion.current && sequence === commandSequence.current) {
        const message = error instanceof Error ? error.message : 'The request failed.';
        setPanel(bare.startsWith('/rename ') ? {
          title: 'Rename session', rename: true, draft: requestLine.slice(7).trim(), message, feedback: 'error',
        } : {
          title: readTitle ?? 'Command failed',
          actions: readTitle ? [{ label: 'Retry', line: requestLine }] : undefined,
          feedback: 'error',
          message,
        });
      }
    } finally {
      finishEdit?.();
    }
  }, [beginEdit, catalog, dialog, open, checkingSelection, navigate, openFiles, openReview, refreshHealth, refreshSessions, reloadHistory, update]);

  const syncQueue = useCallback(async () => {
    if (navigation.current) return;
    const version = navigationVersion.current;
    const state = await readQueue();
    if (version !== navigationVersion.current || navigation.current) return;
    reconcileSelection(state.session_id);
    if (state.archived_session_id && state.archived_session_id === chatRef.current.sessionId) {
      forget(state.archived_session_id);
      update(() => addInfo(initialChatState, 'This conversation was moved to Archive.'));
      changeQueue([]);
      setQueuePaused(false);
      void refreshSessions();
      return;
    }
    if (state.session_id === null || state.session_id !== chatRef.current.sessionId) return;
    changeQueue(state.waiting);
    setQueuePaused(state.paused);
    if (state.active) void observe(state.active);
    else if (!stream.current && revision.current?.session === state.session_id && revision.current.value !== state.revision) {
      revision.current = { session: state.session_id, value: state.revision };
      await reloadHistory(state.session_id);
      return;
    }
    revision.current = { session: state.session_id, value: state.revision };
  }, [changeQueue, forget, navigation, observe, readQueue, reconcileSelection, refreshSessions, reloadHistory, stream, update]);

  syncRef.current = syncQueue;
  const removeQueued = useCallback((index: number) => {
    const item = queueRef.current[index];
    if (item) void removeQueuedTurn(item.turn_id).then(syncQueue).catch(() => undefined);
  }, [syncQueue]);

  const discardFailedEdit = useCallback((sessionId: string, editId?: string) => {
    const saved = failedEditsRef.current[sessionId];
    if (editId && saved?.draft.editId && saved.draft.editId !== editId) return;
    forgetEdit(sessionId, editId ?? saved?.draft.editId);
    const next = { ...failedEditsRef.current };
    delete next[sessionId];
    failedEditsRef.current = next;
    setFailedEdits(next);
  }, []);

  const openMessageEditor = useCallback(async (
    sessionId: string, messageId: string, input: EditedDraft, warning?: string,
  ) => {
    const available = () => {
      const state = chatRef.current;
      const latest = [...state.messages].reverse().find(m => m.role === 'user');
      return state.sessionId === sessionId && latest?.savedId === messageId && !failedEditsRef.current[sessionId];
    };
    if (!available()) return;
    const editId = newRequestId();
    const edit: FailedEdit = { sessionId, messageId, draft: { ...input, editId }, mode: 'compose', dirty: false, error: '', warning };
    if (input.files?.length) {
      // 首次按键前先保存附件副本，这样即使编辑后立刻按 Ctrl+R 也能还原。
      edit.warning = persistEdit(edit).warning;
      if (edit.warning !== tabOnly) {
        try {
          await saveEditFiles(editId, input.attachments.map(item => item.id), input.files);
          edit.draft.filesStored = true;
        } catch { edit.warning = missingFiles; }
      }
    }
    if (available()) showFailedEdit(edit);
    else forgetEdit(sessionId, editId);
  }, [showFailedEdit]);

  const reviseFailedEdit = useCallback((sessionId: string, draft: EditedDraft) => {
    const saved = failedEditsRef.current[sessionId];
    if (saved && saved.mode !== 'retry') showFailedEdit(persistEdit({ ...saved, draft, dirty: true }));
  }, [showFailedEdit]);

  const latestUser = [...chat.messages].reverse().find(m => m.role === 'user');
  useEffect(() => {
    if (failedEdit?.mode !== 'compose' || !latestUser?.savedId || latestUser.savedId === failedEdit.messageId || undoing) return;
    if (!failedEdit.dirty) {
      discardFailedEdit(failedEdit.sessionId);
    } else {
      showFailedEdit(persistEdit({ ...failedEdit, mode: 'stale', error: 'A newer message is now the last turn. Your edit is saved here.' }));
    }
  }, [discardFailedEdit, failedEdit, latestUser?.savedId, showFailedEdit, undoing]);

  const actOnLastMessage = useCallback(async (
    sessionId: string, messageId: string, replacement?: EditedDraft,
  ) => {
    if (!replacement) {
      const result = await runCommand('/undo', false, undefined, { sessionId, messageId });
      return result?.status === 'ok' && typeof result.data?.restored === 'string';
    }
    if (editing.current || navigation.current || chatRef.current.sessionId !== sessionId
      || (checkingSelection && offlineRecoverySession.current !== sessionId)) return false;
    const finishEdit = beginEdit();
    const editId = replacement.editId ?? newRequestId();
    let draft: EditedDraft = { ...replacement, editId };
    const previous = failedEditsRef.current[sessionId];
    const pending = 'Delivery could not be confirmed. Retry this edit to check its receipt.';
    let warning = persistEdit(previous ?? {
      sessionId, messageId, draft, mode: 'edit', error: 'This edit was interrupted before it was submitted.',
    }).warning;
    try {
      // 替换成功可能使原下载链接失效，应提前保存可恢复的原文件，
      // 也要覆盖标签页在 POST 期间重载的情况。
      if (offlineRecoverySession.current === sessionId) {
        await api.switchSession(sessionId);
        offlineRecoverySession.current = null;
      }
      let files = draft.files;
      if (!files && draft.filesStored) files = await readEditFiles(editId, draft.attachments).catch(() => undefined);
      files ??= await Promise.all(draft.attachments.map(item => api.readUpload(sessionId, item)));
      draft = { ...draft, files };
      if (files.length && !draft.filesStored && warning !== tabOnly) {
        try {
          await saveEditFiles(editId, draft.attachments.map(item => item.id), files);
          draft = { ...draft, filesStored: true };
          warning = undefined;
        } catch {
          warning = missingFiles;
        }
      }
      warning = persistEdit({
        sessionId, messageId, draft: { ...draft, receiptUnknown: true }, mode: 'retry', error: pending, warning,
      }).warning;
      if (!navigation.current && chatRef.current.sessionId === sessionId) setSendRevision(value => value + 1);
      await submitChat({ session_id: sessionId, message: draft.text }, files, messageId, editId);
      discardFailedEdit(sessionId, editId);
    } catch (error) {
      const code = error instanceof ApiError ? error.code : undefined;
      if (draft.files && !code) draft = { ...draft, receiptUnknown: true };
      if (code === 'edit_not_sent') draft = { ...draft, receiptUnknown: false };
      const rejected = ['edit_not_sent', 'invalid_upload', 'empty_message', 'command_required', 'upload_too_large'].includes(code ?? '');
      const stale = ['edit_conflict', 'session_mismatch', 'session_not_found'].includes(code ?? '');
      const mode = stale ? 'stale' : !draft.receiptUnknown && (!draft.files || rejected) ? 'edit' : 'retry';
      const message = error instanceof ApiError ? error.message : draft.files ? pending
        : error instanceof Error ? error.message : 'Could not read the original attachments.';
      showFailedEdit(persistEdit({ sessionId, messageId, draft, error: message, mode, warning }));
    } finally {
      finishEdit();
      if (chatRef.current.sessionId === sessionId) {
        if (!stream.current) void reloadHistory(sessionId).then(syncQueue).catch(() => undefined);
        else void syncQueue().catch(() => undefined);
      }
    }
    // 将失败的草稿保存在原消息行之外，因为其他客户端或本端已受理的请求可能已移除该行。
    return true;
  }, [beginEdit, checkingSelection, discardFailedEdit, reloadHistory, runCommand, showFailedEdit, stream, syncQueue]);

  const showReceipt = useCallback((receipt: TurnReceipt) => {
    if (!chatRef.current.sessionId) {
      assignDraft?.(blankDraftId.current, receipt.session_id);
      update(prev => ({ ...prev, sessionId: receipt.session_id }));
    }
    if (navigation.current) return; // 目标会话会自行同步队列。
    if (receipt.history_only) {
      void reloadHistory(receipt.session_id);
      return;
    }
    const version = navigationVersion.current;
    const current = () => !navigation.current && version === navigationVersion.current && chatRef.current.sessionId === receipt.session_id;
    void readQueue().then(state => {
      if (!current() || state.session_id !== receipt.session_id) return;
      changeQueue(state.waiting);
      setQueuePaused(state.paused);
      if (!stream.current) void observe(state.active ?? receipt);
    }).catch(() => {
      // 请求已受理，重新连接事件流即可，避免重复提交。
      if (current() && !stream.current) void observe(receipt);
    });
  }, [assignDraft, blankDraftId, changeQueue, navigation, observe, readQueue, reloadHistory, stream, update]);
  const deliveries = useDeliveries(currentSession, showReceipt, deliveryFailed, queueSubmission);
  const deliver = deliveries.send;
  const sendDelivery = useCallback((...args: Parameters<typeof deliver>) => {
    setSendRevision(value => value + 1);
    return deliver(...args);
  }, [deliver]);
  const sendAfterSelection = preview?.chat.sessionId ? (text: string, files: readonly File[], handedOff: () => void) => {
    const id = preview.chat.sessionId!;
    return submitWhenSelected(() => committedKey.current === preview.key && currentSession() === id, async () => {
      const delivery = sendDelivery(id, text, files);
      handedOff();
      await delivery;
    });
  } : undefined;
  const send = useCallback((text: string, files: readonly File[] = []): Promise<void> => {
    const trimmed = text.trim();
    if (!trimmed) return Promise.resolve();
    if (isCommandLine(trimmed)) return runCommand(trimmed).then(() => {});
    return queueSubmission(() => sendDelivery(chatRef.current.sessionId, trimmed, files));
  }, [queueSubmission, runCommand, sendDelivery]);

  useEffect(() => {
    if (!sidebarOpen && !chat.sessionId && !checkingSelection) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try { await syncQueue(); } catch { /* 下次刷新界面时重试。 */ }
      if (!stopped) timer = setTimeout(refresh, 1000);
    };
    void refresh();
    return () => { stopped = true; clearTimeout(timer); };
  }, [chat.sessionId, checkingSelection, sidebarOpen, syncQueue]);

  const insert = useCallback(async (text: string) => {
    if (navigation.current || checkingSelection) return false;
    const version = navigationVersion.current;
    const turnId = stream.current?.turnId ?? chatRef.current.turnId;
    if (!chatRef.current.busy || turnId == null || editing.current) {
      update((prev) => ({ ...prev, error: 'Cannot insert now. Enter queues the message while a reply is running.' }));
      return false;
    }
    try {
      await api.insertMessage(turnId, chatRef.current.sessionId, text);
      if (version === navigationVersion.current) setSendRevision(value => value + 1);
      return true;
    } catch (error) {
      if (version === navigationVersion.current) {
        update((prev) => ({ ...prev, error: error instanceof Error ? error.message : 'The message was not inserted.' }));
      }
      return false;
    }
  }, [checkingSelection, navigation, stream, update]);

  const stop = useCallback(async () => {
    if (navigation.current || checkingSelection) return;
    const version = navigationVersion.current;
    const turnId = stream.current?.turnId ?? chatRef.current.turnId;
    if (turnId == null) return;
    try {
      await api.cancelTurn(turnId);
    } catch {
      if (version === navigationVersion.current) update((prev) => ({ ...prev, error: '停止请求失败，回复仍可能在生成' }));
    }
  }, [checkingSelection, navigation, stream, update]);

  const newSession = useCallback(() => navigate(null), [navigate]);
  const selectSession = useCallback((sessionId: string) => {
    if (!navigation.current && !checkingSelection && chatRef.current.sessionId === sessionId && offlineRecoverySession.current !== sessionId) {
      if (window.innerWidth < 768) setSidebarOpen(false);
      return Promise.resolve(true);
    }
    return navigate(sessionId);
  }, [checkingSelection, navigate, navigation]);

  const archiveSession = useCallback(async (session: SessionInfo) => {
    try {
      await api.archiveSession(session.id);
      forget(session.id);
      if (chatRef.current.sessionId === session.id) {
        update(() => addInfo(initialChatState, 'This conversation was moved to Archive.'));
        changeQueue([]);
        setQueuePaused(false);
      }
      await refreshSessions();
    } catch (error) {
      update(prev => ({ ...prev, error: error instanceof Error ? error.message : 'Archiving failed.' }));
    }
  }, [changeQueue, forget, refreshSessions, update]);

  const requestDeleteSession = useCallback((session: SessionInfo, notice = '', previous?: DeleteTarget) => {
    const target: DeleteTarget = { ...session, preview: null, notice };
    if (previous) {
      setDialog(current => current?.kind === 'delete' && current.target === previous ? { kind: 'delete', target } : current);
    } else {
      open({ kind: 'delete', target });
    }
    void api.deletionPreview(session.id).then(
      preview => setDialog(current => current?.kind === 'delete' && current.target === target
        ? { kind: 'delete', target: { ...target, preview } } : current),
      error => setDialog(current => current?.kind === 'delete' && current.target === target
        ? { kind: 'delete', target: { ...target, notice: error instanceof Error ? error.message : 'Could not load related pending items.' } }
        : current),
    );
  }, [open, setDialog]);

  const confirmDeleteSession = useCallback(async () => {
    const target = deleteTarget;
    if (!target?.preview) return;
    const deleting = { ...target, preview: null, notice: 'Deleting…' };
    open({ kind: 'delete', target: deleting });
    try {
      await api.deleteSession(target.id, target.preview.revision);
      forget(target.id);
      setDialog(current => current?.kind === 'delete' && current.target === deleting ? { kind: 'archive' } : current);
      discardFailedEdit(target.id);
      await refreshSessions();
    } catch (error) {
      if (error instanceof ApiError && error.code === 'pending_changed') {
        requestDeleteSession(target, error.message, deleting);
        return;
      }
      setDialog(current => current?.kind === 'delete' && current.target === deleting
        ? { kind: 'delete', target: { ...target, notice: error instanceof Error ? error.message : 'Deletion failed.' } }
        : current);
    }
  }, [deleteTarget, discardFailedEdit, forget, refreshSessions, requestDeleteSession]);

  useEffect(() => {
    let alive = true;
    const version = navigationVersion.current;
    const sessionListVersion = sessionsVersion.current;
    void (async () => {
      try {
        let saved: string | null = null;
        try { saved = window.sessionStorage.getItem('syvrio.session'); } catch { /* 此项可选。 */ }
        if (saved && failedEditsRef.current[saved] && version === navigationVersion.current) {
          // 网关恢复连接前，用户也能阅读本地恢复的编辑内容。
          offlineRecoverySession.current = saved;
          update(prev => ({ ...prev, sessionId: saved }));
        }
        if (saved) await navigate(saved);
        if (!alive) return;
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
        let historyId = listing.items.find((item) => item.current)?.id;
        if (info.active_turn_id != null && version === navigationVersion.current) {
          const state = await readQueue();
          if (!alive || version !== navigationVersion.current) return;
          if (state.active) {
            update(prev => ({ ...prev, sessionId: state.session_id }));
            if (state.session_id) loadSelectionHistory(state.session_id);
            void observe(state.active);
            return;
          }
          historyId = state.session_id ?? historyId;
        }
        if (historyId && version === navigationVersion.current && !chatRef.current.busy) {
          const history = await api.history(historyId);
          if (alive && version === navigationVersion.current && !chatRef.current.busy && history.session_id === historyId) {
            update(() => remember(history));
          }
        }
      } catch {
        if (alive) setOnline(false);
      }
    })();
    const goOnline = () => setOnline(true);
    const goOffline = () => setOnline(false);
    const onPageHide = (event: PageTransitionEvent) => {
      if (!event.persisted) void api.disconnect().catch(() => undefined);
    };
    window.addEventListener('pagehide', onPageHide);
    window.addEventListener('online', goOnline);
    window.addEventListener('offline', goOffline);
    return () => {
      alive = false;
      window.removeEventListener('online', goOnline);
      window.removeEventListener('offline', goOffline);
      window.removeEventListener('pagehide', onPageHide);
      detach();
    };
  }, [detach, loadSelectionHistory, navigate, observe, readQueue, remember, update]);

  const toggleSidebar = useCallback(() => setSidebarOpen((open) => !open), []);
  const openDialog = useCallback((next: Parameters<typeof open>[0], trigger?: HTMLElement) => {
    ++commandSequence.current;
    open(next, trigger);
  }, [open]);
  const focusComposer = useCallback(() => setFocusSignal((value) => value + 1), []);
  const closeDialog = useCallback(() => {
    ++commandSequence.current;
    close();
  }, [close]);
  const handleEscape = useCallback(() => {
    if (dialog) { closeDialog(); return true; }
    if (!navigation.current && chatRef.current.busy) { void stop(); return true; }
    return false;
  }, [dialog, closeDialog, navigation, stop]);

  const confirmCommand = useCallback(async () => {
    if (!confirm) return;
    const { line } = confirm;
    setDialog(null);
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

  return {
    chat,
    sendRevision, preview: preview?.chat, loadingHistory, selectionVersion: preview?.key ?? committedKey.current, sendAfterSelection,
    composerDraftId: preview?.draftId ?? chat.sessionId ?? blankDraftId.current,
    checkingSelection,
    deliveries,
    contextUsage: usage?.sessionId === chat.sessionId ? usage.value : undefined,
    compaction,
    sessions,
    replyingSessionIds,
    health,
    online,
    sidebarOpen,
    setSidebarOpen,
    toggleSidebar,
    refreshSessions,
    renameSession,
    send,
    insert,
    queued: queued.map(item => item.text),
    queuePaused,
    removeQueued,
    undoing,
    actOnLastMessage,
    failedEdit,
    discardFailedEdit,
    reviseFailedEdit,
    openMessageEditor,
    undoDraft: chat.sessionId === null ? undefined : undoDrafts[chat.sessionId],
    acknowledgeDraft,
    stop,
    newSession,
    selectSession,
    catalog,
    dialog,
    openDialog,
    closeDialog,
    confirmCommand,
    requestDeleteSession,
    archiveSession,
    confirmDeleteSession,
    refreshHealth,
    reviewMemory,
    runCommand,
    handleEscape,
    focusSignal,
    focusComposer,
    toggle: preview ? togglePreview : (segmentId: string) => update((prev) => toggleSegment(prev, segmentId)),
    dismissError: () => update((prev) => ({ ...prev, error: null })),
  };
}
