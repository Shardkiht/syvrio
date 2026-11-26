import { testAttachmentPolicy } from '../../test/attachmentPolicy';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import * as client from '../../api/client';
import { api } from '../../api/client';
import type { HistoryResponse, StreamEvent } from '../../types';
import { useChat } from './useChat';
import { sessionHistory, switchResponse } from '../../test/sessionHistory';

afterEach(() => vi.restoreAllMocks());

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

it('refreshes a pending title and stops polling once the generated title arrives', async () => {
  vi.useFakeTimers();
  try {
    vi.spyOn(api, 'health').mockResolvedValue({
      status: 'ok', version: 'test', provider: 'test', model: 'test', busy: false,
      active_turn_id: null, attachment_policy: testAttachmentPolicy, static_ready: true, single_client: false,
    });
    vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
    const row = { id: 'a', title: 'Untitled chat', messages: 1, current: false,
      started_at: '', updated_at: '', title_pending: true };
    const list = vi.spyOn(api, 'listSessions').mockResolvedValueOnce({ items: [row] })
      .mockResolvedValue({ items: [{ ...row, title: 'Planning a lighthouse visit', title_pending: false }] });
    const { result, unmount } = renderHook(() => useChat());
    await act(async () => {});
    expect(result.current.sessions[0]?.title).toBe('Untitled chat');
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(result.current.sessions[0]?.title).toBe('Planning a lighthouse visit');
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(list).toHaveBeenCalledTimes(2);
    unmount();
  } finally {
    vi.useRealTimers();
  }
});

it('serializes switches, skips superseded requests, and ignores stale history', async () => {
  vi.spyOn(api, 'health').mockRejectedValue(new Error('probe unavailable'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const first = deferred<ReturnType<typeof switchResponse>>();
  const switchSession = vi.spyOn(api, 'switchSession')
    .mockImplementation((id) => id === 'a' ? first.promise : Promise.resolve(switchResponse(sessionHistory(id, 'Latest'))));
  const history = vi.spyOn(api, 'history');
  const { result } = renderHook(() => useChat());
  act(() => { void result.current.selectSession('a'); });
  await waitFor(() => expect(switchSession).toHaveBeenCalledWith('a'));
  act(() => {
    void result.current.selectSession('b');
    void result.current.selectSession('c');
  });
  expect(switchSession).toHaveBeenCalledTimes(1);
  expect(result.current.preview?.sessionId).toBe('c');
  await act(async () => first.resolve(switchResponse(sessionHistory('a', 'Old'))));
  await waitFor(() => expect(result.current.chat.sessionId).toBe('c'));
  expect(switchSession.mock.calls).toEqual([['a'], ['c']]);
  expect(result.current.chat.messages[0]).toMatchObject({ content: 'Latest' });
  expect(history).not.toHaveBeenCalled();
});

it('keeps the current turn when the server reports /new is busy', async () => {
  vi.spyOn(api, 'health').mockRejectedValue(new Error('probe unavailable'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'switchSession').mockResolvedValue(switchResponse(sessionHistory('a')));
  vi.spyOn(api, 'history').mockResolvedValue({
    session_id: 'a', title: 'a', items: [], summary: null, has_earlier: false, current: true,
  });
  vi.spyOn(api, 'runCommand').mockResolvedValue({
    status: 'busy', command: 'new', intent: 'new_session', message: 'A reply is being generated.',
    session_id: 'a', data: null, refresh_history: false, refresh_sessions: false,
  });
  const { result } = renderHook(() => useChat());
  await act(async () => { await result.current.selectSession('a'); });
  await act(async () => { await result.current.runCommand('/new'); });
  expect(result.current.chat.sessionId).toBe('a');
});

it('finishes restoring a tab before a newly selected chat can change its binding', async () => {
  window.sessionStorage.setItem('syvrio.session', 'saved');
  try {
    const restore = deferred<void>();
    let bound: string | null = null;
    const switchSession = vi.spyOn(api, 'switchSession').mockImplementation(async id => {
      if (id === 'saved') await restore.promise;
      bound = id;
      return switchResponse(sessionHistory(id));
    });
    vi.spyOn(api, 'health').mockResolvedValue({ status: 'ok', version: 'test', provider: 'spark', model: 'test',
      busy: false, active_turn_id: null, attachment_policy: testAttachmentPolicy, static_ready: true, single_client: false });
    vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
    vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
    vi.spyOn(api, 'history').mockImplementation(async id => ({ session_id: id, title: id, items: [],
      current: true, summary: null, has_earlier: false }));
    vi.spyOn(client, 'queueState').mockImplementation(async () => ({
      session_id: bound, active: null, waiting: [], revision: 0, paused: false,
    }));
    const { result } = renderHook(() => useChat());
    await waitFor(() => expect(switchSession).toHaveBeenCalledWith('saved'));
    await act(async () => { void result.current.selectSession('clicked'); });
    expect(switchSession.mock.calls).toEqual([['saved']]);
    await act(async () => { restore.resolve(); });
    await waitFor(() => expect(result.current.chat.sessionId).toBe('clicked'));
    expect(bound).toBe('clicked');
    expect(result.current.health?.provider).toBe('spark');
  } finally { window.sessionStorage.removeItem('syvrio.session'); }
});

it.each(['sidebar', 'button', 'command'])('leaves a streaming conversation through %s and resumes its own history and queue', async entry => {
  const a = { turn_id: 11, session_id: 'a', text: 'Current question', message_id: 'u11' };
  const b = { turn_id: 22, session_id: 'b', text: 'B question', message_id: 'u22' };
  const queued = { turn_id: 12, session_id: 'a', text: 'Queued in A' };
  const turns: Record<string, typeof a> = { a, b };
  let selected: string | null = 'a';
  vi.spyOn(api, 'health').mockRejectedValue(new Error('unused'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'switchSession').mockImplementation(async sid => {
    selected = sid;
    return switchResponse(transcript(sid));
  });
  vi.spyOn(api, 'createSession').mockImplementation(async () => {
    selected = null; return { session_id: null, title: '' };
  });
  vi.spyOn(api, 'runCommand').mockImplementation(async () => {
    selected = null;
    return { status: 'ok', command: 'new', intent: 'new_session', message: '', data: null,
      session_id: null, refresh_history: true, refresh_sessions: true };
  });
  const transcript = (id: string): HistoryResponse => ({
    ...sessionHistory(id),
    items: ['Earlier in ' + id, turns[id].text].map((content, i) => ({
      id: i ? turns[id].message_id : id+'-earlier', role: 'user', content, at: '',
      attachments: [], reasoning: null, tool_calls: [], tool_name: null, tool_ok: null, interrupted: false,
    })),
  });
  vi.spyOn(api, 'history').mockImplementation(async id => transcript(id));
  vi.spyOn(client, 'queueState').mockImplementation(async () => ({
    session_id: selected, active: selected ? turns[selected] : null, waiting: selected === 'a' ? [queued] : [],
    revision: 1, paused: false,
  }));
  const listeners: { turn: number; emit: (event: StreamEvent) => void; end: () => void; signal?: AbortSignal }[] = [];
  vi.spyOn(client, 'replayTurn').mockImplementation(async (turn, after, emit, signal) => {
    expect(after).toBe(0);
    const finished = deferred<void>();
    listeners.push({ turn, emit, end: () => finished.resolve(), signal });
    emit({ type: 'start', seq: 1, turn_id: turn, session_id: turn === 11 ? 'a' : 'b' });
    await finished.promise;
  });
  const cancel = vi.spyOn(api, 'cancelTurn').mockResolvedValue({ cancelled: true, turn_id: 22, active_turn_id: 22 });
  const submit = vi.spyOn(client, 'submitChat');
  const { result, unmount } = renderHook(() => useChat());
  await act(async () => { await result.current.selectSession('a'); });
  await waitFor(() => expect(result.current.chat.busy).toBe(true));
  expect(result.current.queued).toEqual([queued.text]);
  const old = listeners[0];
  await act(async () => { await result.current.selectSession('a'); });
  expect(old.signal?.aborted).toBe(false);
  await act(async () => {
    old.emit({ type: 'content_delta', seq: 2, turn_id: 11, session_id: 'a', text: 'buffered A' });
    if (entry === 'button') await result.current.newSession();
    else if (entry === 'command') await result.current.runCommand('/new');
    else await result.current.selectSession('b');
  });
  expect(old.signal?.aborted).toBe(true);
  expect(result.current.queued).toEqual([]);
  expect(cancel).not.toHaveBeenCalled();
  if (entry !== 'sidebar') {
    expect(result.current.chat.sessionId).toBeNull();
    expect(result.current.chat.busy).toBe(false);
    await act(async () => { await result.current.selectSession('b'); });
  }
  await waitFor(() => expect(result.current.chat.turnId).toBe(22));
  await act(async () => {
    old.emit({ type: 'done', seq: 3, turn_id: 11, session_id: 'a', text: 'late A' });
    old.end();
    await result.current.stop();
  });
  expect(cancel.mock.calls).toEqual([[22]]);
  expect(result.current.chat.sessionId).toBe('b');
  expect(JSON.stringify(result.current.chat.messages)).not.toMatch(/buffered A|late A|Earlier in a/);
  await act(async () => { await result.current.selectSession('a'); });
  await waitFor(() => expect(result.current.chat.turnId).toBe(11));
  expect(result.current.queued).toEqual([queued.text]);
  expect(result.current.chat.messages.filter(m => m.role === 'user').map(m => m.content))
    .toEqual(['Earlier in a', 'Current question']);
  expect(submit).not.toHaveBeenCalled();
  unmount();
  listeners.forEach(l => l.end());
});


it('keeps an unacknowledged first send in its original conversation when New chat is clicked', async () => {
  vi.spyOn(api, 'health').mockRejectedValue(new Error('unused'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const admitted = deferred<client.TurnReceipt>();
  const submit = vi.spyOn(client, 'submitChat').mockReturnValue(admitted.promise);
  const create = vi.spyOn(api, 'createSession').mockResolvedValue({ session_id: null, title: '' });
  vi.spyOn(api, 'history').mockResolvedValue({ session_id: 'original', title: '', items: [], current: true, summary: null, has_earlier: false });
  vi.spyOn(client, 'queueState').mockResolvedValue({ session_id: null, waiting: [], active: null, paused: false, revision: 0 });
  const { result } = renderHook(() => useChat());
  act(() => { void result.current.send('First'); });
  await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
  act(() => { void result.current.newSession(); });
  expect(create).not.toHaveBeenCalled();
  await act(async () => { admitted.resolve({ turn_id: 41, session_id: 'original', text: 'First' }); });
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  expect(result.current.chat.sessionId).toBeNull();
  expect(result.current.chat.messages).toEqual([]);
  expect(submit).toHaveBeenCalledTimes(1);
});

it('shows the uncached destination immediately and reads it once after first-message admission', async () => {
  vi.spyOn(api, 'health').mockRejectedValue(new Error('unused'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const admitted = deferred<client.TurnReceipt>();
  vi.spyOn(client, 'submitChat').mockReturnValue(admitted.promise);
  const bound = deferred<ReturnType<typeof switchResponse>>();
  const select = vi.spyOn(api, 'switchSession').mockReturnValue(bound.promise);
  const queue = deferred<Awaited<ReturnType<typeof client.queueState>>>();
  vi.spyOn(client, 'queueState').mockReturnValue(queue.promise);
  const history = vi.spyOn(api, 'history');
  const { result } = renderHook(() => useChat());
  act(() => { void result.current.send('First'); });
  await waitFor(() => expect(client.submitChat).toHaveBeenCalledOnce());
  act(() => { void result.current.selectSession('b'); });
  expect(result.current.preview?.sessionId).toBe('b');
  expect(result.current.loadingHistory).toBe(true);
  expect(history).not.toHaveBeenCalled();
  expect(select).not.toHaveBeenCalled();
  await act(async () => { admitted.resolve({ turn_id: 41, session_id: 'a', text: 'First' }); });
  await waitFor(() => expect(select).toHaveBeenCalledWith('b'));
  expect(result.current.chat.sessionId).toBe('a');
  expect(result.current.preview?.sessionId).toBe('b');
  await act(async () => { bound.resolve(switchResponse(sessionHistory('b', 'Edited while waiting'))); });
  expect(history).not.toHaveBeenCalled();
  expect(result.current.preview).toBeUndefined();
  expect(result.current.chat.messages[0]).toMatchObject({ content: 'Edited while waiting' });
  await act(async () => { queue.resolve({ session_id: 'a', active: null, waiting: [], paused: false, revision: 1 }); });
  expect(result.current.chat.sessionId).toBe('b');
  expect(client.submitChat).toHaveBeenCalledOnce();
});

it.each([true, false])('recovers the actual selection when the switch was accepted but its response was lost (online=%s)', async (online) => {
  window.sessionStorage.removeItem('syvrio.session');
  vi.spyOn(api, 'health').mockRejectedValue(new Error('offline at startup'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const request = vi.spyOn(api, 'switchSession').mockRejectedValue(new client.NetworkError('Reply lost'));
  let reachable = online;
  vi.spyOn(client, 'queueState').mockImplementation(async () => {
    if (!reachable) throw new client.NetworkError('Offline');
    return { session_id: 'target', active: null, waiting: [], revision: 0, paused: false };
  });
  const history = vi.spyOn(api, 'history').mockResolvedValue(sessionHistory('target', 'Recovered target'));
  const { result } = renderHook(() => useChat());
  await act(async () => { await result.current.selectSession('target'); });
  if (!online) {
    expect(result.current.checkingSelection).toBe(true);
    reachable = true;
  }
  await waitFor(() => expect(result.current.chat.sessionId).toBe('target'), { timeout: 2500 });
  expect(result.current.checkingSelection).toBe(false);
  expect(request).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(result.current.chat.messages[0]).toMatchObject({ content: 'Recovered target' }));
  expect(history.mock.calls).toEqual([['target']]);
  expect(result.current.chat.error).toBeNull();
});

it('detaches the server view as well as the transcript on /exit', async () => {
  vi.spyOn(api, 'health').mockRejectedValue(new Error('unused'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'switchSession').mockResolvedValue(switchResponse(sessionHistory('previous')));
  vi.spyOn(api, 'history').mockResolvedValue({ session_id: 'previous', title: '', items: [], current: true, summary: null, has_earlier: false });
  vi.spyOn(api, 'runCommand').mockResolvedValue({ status: 'client', command: 'exit', intent: 'leave', message: 'Leave',
    data: null, session_id: 'previous', refresh_history: false, refresh_sessions: false });
  vi.spyOn(client, 'queueState').mockResolvedValue({ session_id: null, waiting: [], active: null, paused: false, revision: 0 });
  const create = vi.spyOn(api, 'createSession').mockResolvedValue({ session_id: null, title: '' });
  const { result } = renderHook(() => useChat());
  await act(async () => { await result.current.selectSession('previous'); });
  await act(async () => { await result.current.runCommand('/exit'); });
  expect(create).toHaveBeenCalledTimes(1);
  expect(result.current.chat.sessionId).toBeNull();
});

it.each(['after-turn', null])('uses only the version attached to refreshed history (header=%s)', async version => {
  vi.spyOn(api, 'health').mockRejectedValue(new Error('unused'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const earlier = sessionHistory('a', 'Original history');
  const later = sessionHistory('a', 'Saved answer');
  const reread = vi.spyOn(globalThis, 'fetch').mockImplementation(async path => {
    expect(String(path)).toBe('/api/sessions/a?include_version=true');
    return new Response(JSON.stringify(later), {
      headers: version ? { 'X-Syvrio-History-Version': version } : {},
    });
  });
  const select = vi.spyOn(api, 'switchSession')
    .mockResolvedValueOnce(switchResponse(earlier, 'before-turn'))
    .mockResolvedValueOnce(switchResponse(sessionHistory('b'), 'b1'))
    .mockResolvedValueOnce({ ...switchResponse(later, version ?? 'current'), history: version ? null : later });
  const ended = deferred<void>();
  const turn = { turn_id: 22, session_id: 'a', text: 'In progress' };
  const queue = vi.spyOn(client, 'queueState').mockResolvedValue({
    session_id: 'a', active: turn, waiting: [], revision: 0, paused: false,
  });
  const replay = vi.spyOn(client, 'replayTurn').mockImplementation(async (_id, _seq, emit) => {
    await ended.promise;
    emit({ type: 'done', turn_id: 22, seq: 1, text: 'Saved answer' });
  });
  const { result } = renderHook(() => useChat());
  await act(async () => { await result.current.selectSession('a'); });
  await waitFor(() => expect(replay).toHaveBeenCalledOnce());
  queue.mockResolvedValue({ session_id: 'a', active: null, waiting: [], revision: 1, paused: false });
  await act(async () => { ended.resolve(); });
  await waitFor(() => expect(result.current.chat.messages[0]).toMatchObject({ content: 'Saved answer' }));
  await act(async () => { await result.current.selectSession('b'); });
  await act(async () => { await result.current.selectSession('a'); });
  expect(select.mock.calls.at(-1)).toEqual(version ? ['a', version] : ['a']);
  expect(result.current.chat.messages[0]).toMatchObject({ content: 'Saved answer' });
  expect(reread).toHaveBeenCalled();
});

it('reattaches to the active turn when the page opens and then reloads saved history', async () => {
  const turn = { turn_id: 42, session_id: 'new-session', text: 'original', message_id: 'current' };
  const state = { session_id: turn.session_id, active: turn, waiting: [], revision: 0, paused: false };
  const finished = deferred<void>();
  vi.spyOn(api, 'health').mockResolvedValue({
    status: 'ok', version: 'test', provider: 'test', model: 'test', busy: true,
    active_turn_id: 42, attachment_policy: testAttachmentPolicy, static_ready: true, single_client: false,
  });
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const queue = vi.spyOn(client, 'queueState').mockResolvedValue(state);
  const replay = vi.spyOn(client, 'replayTurn').mockImplementation(async (_id, _after, emit) => {
    emit({ type: 'start', seq: 1, turn_id: 42, session_id: turn.session_id });
    await finished.promise;
    emit({ type: 'done', seq: 2, turn_id: 42, text: 'Completed in background' });
  });
  const history = vi.spyOn(api, 'history').mockResolvedValueOnce({
    session_id: turn.session_id, title: 'Test', items: [
      { id: 'previous', role: 'user', content: 'Earlier question', at: '',
        reasoning: null, tool_calls: [], tool_name: null, tool_ok: null, interrupted: false },
      { id: 'current', role: 'user', content: 'original', at: '',
        reasoning: null, tool_calls: [], tool_name: null, tool_ok: null, interrupted: false },
    ], summary: null, has_earlier: false, current: true,
  }).mockResolvedValue({
    session_id: turn.session_id, title: 'Test', items: [
      { id: 'reply', role: 'assistant', content: 'Completed in background', at: '',
        reasoning: null, tool_calls: [], tool_name: null, tool_ok: null, interrupted: false },
    ], summary: null, has_earlier: false, current: true,
  });
  const { result } = renderHook(() => useChat());
  await waitFor(() => expect(result.current.chat.turnId).toBe(42));
  expect(history).toHaveBeenCalledWith(turn.session_id);
  expect(result.current.chat.messages.filter(m => m.role === 'user').map(m => m.content))
    .toEqual(['Earlier question', 'original']);
  queue.mockResolvedValue({ ...state, active: null, revision: 1 });
  await act(async () => finished.resolve());
  await waitFor(() => expect(result.current.chat.busy).toBe(false));
  expect(replay).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(result.current.chat.messages)).toContain('Completed in background');
});

it('loads saved history if the active reply finishes between the health and queue requests', async () => {
  vi.spyOn(api, 'health').mockResolvedValue({
    status: 'ok', version: 'test', provider: 'test', model: 'test', busy: true,
    active_turn_id: 42, attachment_policy: testAttachmentPolicy, static_ready: true, single_client: false,
  });
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  vi.spyOn(client, 'queueState').mockResolvedValue({ session_id: 'finished', active: null, waiting: [], revision: 1, paused: false });
  const replay = vi.spyOn(client, 'replayTurn');
  vi.spyOn(api, 'history').mockResolvedValue({
    session_id: 'finished', title: 'Test', items: [
      { id: 'reason', role: 'error', content: 'Synthetic timeout', at: '',
        reasoning: null, tool_calls: [], tool_name: null, tool_ok: null, interrupted: false },
    ], summary: null, has_earlier: false, current: true,
  });
  const { result } = renderHook(() => useChat());
  await waitFor(() => expect(result.current.chat.sessionId).toBe('finished'));
  expect(result.current.chat.messages).toEqual([expect.objectContaining({
    role: 'info', content: 'Reply failed: Synthetic timeout',
  })]);
  expect(result.current.chat.busy).toBe(false);
  expect(replay).not.toHaveBeenCalled();
});

it('replays a dropped stream after its last event without resubmitting the message', async () => {
  const turn = { turn_id: 42, session_id: 'original', text: 'Synthetic question' };
  vi.spyOn(api, 'health').mockRejectedValue(new Error('not needed'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const submit = vi.spyOn(client, 'submitChat').mockResolvedValue(turn);
  const state = { session_id: turn.session_id, active: turn, waiting: [], revision: 0, paused: false };
  const queue = vi.spyOn(client, 'queueState').mockResolvedValue(state);
  const replay = vi.spyOn(client, 'replayTurn').mockImplementation(async (_id, after, emit) => {
    if (after === 0) {
      emit({ type: 'start', seq: 1, turn_id: 42, session_id: turn.session_id });
      throw new Error('Connection dropped');
    }
    expect(after).toBe(1);
    queue.mockResolvedValue({ ...state, active: null, revision: 1 });
    emit({ type: 'done', seq: 2, turn_id: 42, text: 'Saved on server' });
  });
  vi.spyOn(api, 'history').mockRejectedValue(new Error('display the replayed reply'));
  const { result } = renderHook(() => useChat());
  await act(async () => { await result.current.send(turn.text); });
  await waitFor(() => expect(JSON.stringify(result.current.chat.messages)).toContain('Saved on server'), { timeout: 2500 });
  expect(replay).toHaveBeenCalledTimes(2);
  expect(submit).toHaveBeenCalledTimes(1);
});
