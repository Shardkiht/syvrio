import { renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import * as client from '../../api/client';
import { useSessionActivity } from './useSessionActivity';

afterEach(() => vi.restoreAllMocks());

function snapshot(session: string, revision: number): client.QueueState {
  return { session_id: session, active: null, waiting: [], paused: false, revision };
}

it.each([
  { first: snapshot('a', 2), later: snapshot('a', 1), winner: 'first' },
  { first: snapshot('a', 6), later: snapshot('b', 0), winner: 'later' },
])('compares overlapping revisions only inside the same session ($winner)', async ({ first, later, winner }) => {
  let finishFirst!: (state: client.QueueState) => void;
  let finishLater!: (state: client.QueueState) => void;
  vi.spyOn(client, 'queueState')
    .mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve; }))
    .mockImplementationOnce(() => new Promise(resolve => { finishLater = resolve; }));
  const { result } = renderHook(() => useSessionActivity([], vi.fn()));
  const earlier = result.current.readQueue();
  const laterRead = result.current.readQueue();
  if (winner === 'first') {
    finishFirst(first);
    await expect(earlier).resolves.toEqual({ state: first, stale: false });
    finishLater(later);
    await expect(laterRead).resolves.toEqual({ state: first, stale: true });
  } else {
    finishLater(later);
    await expect(laterRead).resolves.toEqual({ state: later, stale: false });
    finishFirst(first);
    await expect(earlier).resolves.toEqual({ state: later, stale: true });
  }
});

it('accepts a restarted backend with a lower revision on the next non-overlapping read', async () => {
  const before = snapshot('a', 6);
  const after = snapshot('a', 0);
  vi.spyOn(client, 'queueState').mockResolvedValueOnce(before).mockResolvedValueOnce(after);
  const { result } = renderHook(() => useSessionActivity([], vi.fn()));
  await expect(result.current.readQueue()).resolves.toEqual({ state: before, stale: false });
  await expect(result.current.readQueue()).resolves.toEqual({ state: after, stale: false });
});
