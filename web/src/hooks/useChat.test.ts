import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import * as client from '../api/client';
import { api } from '../api/client';
import type { HistoryResponse } from '../types';
import { useChat } from './useChat';

afterEach(() => vi.restoreAllMocks());

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

it('serializes switches, skips superseded requests, and ignores stale history', async () => {
  vi.spyOn(api, 'health').mockRejectedValue(new Error('probe unavailable'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const first = deferred<{ session_id: string; title: string }>();
  const slowHistory = deferred<HistoryResponse>();
  const switchSession = vi.spyOn(api, 'switchSession')
    .mockImplementation((id) => id === 'a' ? first.promise : Promise.resolve({ session_id: id, title: id }));
  const history = vi.spyOn(api, 'history').mockReturnValue(slowHistory.promise);
  const { result } = renderHook(() => useChat());
  act(() => { void result.current.selectSession('a'); });
  await waitFor(() => expect(switchSession).toHaveBeenCalledWith('a'));
  act(() => {
    void result.current.selectSession('b');
    void result.current.selectSession('c');
  });
  expect(switchSession).toHaveBeenCalledTimes(1);
  await act(async () => first.resolve({ session_id: 'a', title: 'a' }));
  await waitFor(() => expect(result.current.chat.sessionId).toBe('c'));
  expect(switchSession.mock.calls).toEqual([['a'], ['c']]);
  expect(history.mock.calls).toEqual([['c']]);

  vi.spyOn(api, 'createSession').mockResolvedValue({ session_id: null, title: '' });
  await act(async () => { await result.current.newSession(); });
  await act(async () => slowHistory.resolve({
    session_id: 'c', title: 'c', items: [], summary: null, has_earlier: false, current: true,
  }));
  expect(result.current.chat.sessionId).toBeNull();
});

it('keeps the current turn when the server reports /new is busy', async () => {
  vi.spyOn(api, 'health').mockRejectedValue(new Error('probe unavailable'));
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'switchSession').mockResolvedValue({ session_id: 'a', title: 'a' });
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


it('reattaches to the active turn when the page opens and then reloads saved history', async () => {
  const turn = { turn_id: 42, session_id: 'new-session', text: 'original' };
  const state = { session_id: turn.session_id, active: turn, waiting: [], revision: 0, paused: false };
  const finished = deferred<void>();
  vi.spyOn(api, 'health').mockResolvedValue({
    status: 'ok', version: 'test', provider: 'test', model: 'test', busy: true,
    active_turn_id: 42, static_ready: true, single_client: false,
  });
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const queue = vi.spyOn(client, 'queueState').mockResolvedValue(state);
  const replay = vi.spyOn(client, 'replayTurn').mockImplementation(async (_id, _after, emit) => {
    emit({ type: 'start', seq: 1, turn_id: 42, session_id: turn.session_id });
    await finished.promise;
    emit({ type: 'done', seq: 2, turn_id: 42, text: 'Completed in background' });
  });
  const history = vi.spyOn(api, 'history').mockResolvedValue({
    session_id: turn.session_id, title: 'Test', items: [
      { id: 'reply', role: 'assistant', content: 'Completed in background', at: '',
        reasoning: null, tool_calls: [], tool_name: null, tool_ok: null, interrupted: false },
    ], summary: null, has_earlier: false, current: true,
  });
  const { result } = renderHook(() => useChat());
  await waitFor(() => expect(result.current.chat.turnId).toBe(42));
  expect(history).not.toHaveBeenCalled();
  queue.mockResolvedValue({ ...state, active: null, revision: 1 });
  await act(async () => finished.resolve());
  await waitFor(() => expect(result.current.chat.busy).toBe(false));
  expect(replay).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(result.current.chat.messages)).toContain('Completed in background');
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
