import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
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
