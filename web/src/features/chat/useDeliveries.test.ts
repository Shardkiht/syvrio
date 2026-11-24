import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import * as api from '../../api/client';
import { useDeliveries } from './useDeliveries';

afterEach(() => { sessionStorage.clear(); vi.restoreAllMocks(); });

it.each([false, true])('recovers a lost receipt without duplicating a send (accepted=%s)', async acceptedFirst => {
  const receipt = { turn_id: 12, session_id: 'review', text: 'First message' };
  const accepted = vi.fn().mockResolvedValue(undefined);
  const server = new Set<string>();
  let attempts = 0;
  const submit = vi.spyOn(api, 'submitChat').mockImplementation(async body => {
    attempts++;
    if (attempts > 2 || (attempts === 1 && acceptedFirst)) server.add(body.submission_id!);
    if (attempts <= 2) throw new api.NetworkError('Receipt unavailable');
    return receipt;
  });
  const getSession = () => 'review';
  const first = renderHook(() => useDeliveries(getSession, accepted));
  await act(async () => { await first.result.current.send('review', receipt.text, []); });
  expect(first.result.current.items[0].error).toBe('Receipt unavailable');
  first.unmount();
  const second = renderHook(() => useDeliveries(getSession, accepted));
  expect(second.result.current.items[0].text).toBe(receipt.text);
  act(() => { window.dispatchEvent(new Event('online')); });
  await waitFor(() => expect(accepted).toHaveBeenCalledTimes(1));
  expect(new Set(submit.mock.calls.map(([body]) => body.submission_id)).size).toBe(1);
  expect(server.size).toBe(1);
  expect(second.result.current.items).toEqual([]);
});

it('keeps a rejected message and attachment available without retrying a validation error automatically', async () => {
  const submit = vi.spyOn(api, 'submitChat').mockRejectedValue(new api.ApiError('Rejected', 'invalid_upload'));
  const { result } = renderHook(() => useDeliveries(() => null, vi.fn()));
  const file = new File(['source'], 'note.txt', { type: 'text/plain' });
  await act(async () => { await result.current.send(null, 'With file', [file]); });
  expect(submit).toHaveBeenCalledTimes(1);
  expect(result.current.items[0].files).toEqual([file]);
  expect(result.current.items[0].reconnect).toBe(false);
  await act(async () => { await result.current.retry(result.current.items[0]); });
  expect(submit.mock.calls[0]).toEqual(submit.mock.calls[1]);
});
