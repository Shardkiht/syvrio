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


it('waits for a server reply already running when the page opens', async () => {
  const health = {
    status: 'ok', version: 'test', provider: 'test', model: 'test', busy: true,
    active_turn_id: 42, static_ready: true, single_client: true,
  } as const;
  const finished = deferred<import('../types').HealthResponse>();
  vi.spyOn(api, 'health').mockResolvedValueOnce(health).mockReturnValue(finished.promise);
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const history = vi.spyOn(api, 'history').mockResolvedValue({
    session_id: 'new-session', title: 'Test', items: [
      { id: 'reply', role: 'assistant', content: 'Completed in background', at: '',
        reasoning: null, tool_calls: [], tool_name: null, tool_ok: null, interrupted: false },
    ], summary: null, has_earlier: false, current: true,
  });
  const { result } = renderHook(() => useChat());
  await waitFor(() => expect(result.current.chat.turnId).toBe(42));
  expect(result.current.chat.busy).toBe(true);
  expect(history).not.toHaveBeenCalled();
  vi.mocked(api.listSessions).mockResolvedValue({
    items: [{ id: 'new-session', title: 'Test', messages: 1, current: true,
      started_at: '', updated_at: '', last_message_at: '' }],
  });
  await act(async () => finished.resolve({ ...health, active_turn_id: null, busy: false }));
  expect(history).toHaveBeenCalledWith('new-session');
  expect(result.current.chat.busy).toBe(false);
  expect(JSON.stringify(result.current.chat.messages)).toContain('Completed in background');
});


it('reloads the original reply after a dropped stream without resending', async () => {
  const health = {
    status: 'ok', version: 'test', provider: 'test', model: 'test', busy: false,
    active_turn_id: null, static_ready: true, single_client: true,
  } as const;
  vi.spyOn(api, 'health').mockResolvedValue(health);
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [] });
  const cancel = vi.spyOn(api, 'cancelTurn');
  const stream = vi.spyOn(client, 'streamChat').mockImplementation(async (_body, emit) => {
    emit({ type: 'start', seq: 1, session_id: 'original', turn_id: 42 });
    throw new TypeError('Mobile connection dropped');
  });
  const history = vi.spyOn(api, 'history').mockResolvedValue({
    session_id: 'original', title: 'Test', items: [
      { id: 'reply', role: 'assistant', content: 'Saved on server', at: '',
        reasoning: null, tool_calls: [], tool_name: null, tool_ok: null, interrupted: false },
    ], summary: null, has_earlier: false, current: true,
  });
  const { result } = renderHook(() => useChat());
  await waitFor(() => expect(result.current.health?.version).toBe('test'));
  await act(async () => { await result.current.send('Synthetic question'); });
  expect(stream).toHaveBeenCalledTimes(1);
  expect(cancel).not.toHaveBeenCalled();
  expect(history).toHaveBeenCalledWith('original');
  expect(result.current.chat.error).toBeNull();
  expect(JSON.stringify(result.current.chat.messages)).toContain('Saved on server');
});
