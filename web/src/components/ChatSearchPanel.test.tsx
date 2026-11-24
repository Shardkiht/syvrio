import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import { ChatSearchPanel } from './ChatSearchPanel';

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

it('waits for committed IME input, cancels the previous read and clears the results', async () => {
  vi.useFakeTimers();
  const search = vi.spyOn(api, 'searchChats');
  let resolveFirst!: (value: Awaited<ReturnType<typeof api.searchChats>>) => void;
  search.mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve; }))
    .mockResolvedValue({ results: [{ title: 'Current', session_id: 's', message_id: 'm', role: 'user', excerpt: '我是' }] });
  render(<ChatSearchPanel onOpen={() => {}}>Recent chats</ChatSearchPanel>);
  const input = screen.getByRole('searchbox');
  fireEvent.compositionStart(input);
  fireEvent.change(input, { target: { value: 'wo' } });
  await act(async () => { await vi.advanceTimersByTimeAsync(400); });
  expect(search).not.toHaveBeenCalled();
  fireEvent.change(input, { target: { value: '我' } });
  fireEvent.compositionEnd(input);
  await act(async () => { await vi.advanceTimersByTimeAsync(350); });
  expect(search.mock.calls[0][0]).toBe('我');
  fireEvent.change(input, { target: { value: '我是' } });
  expect(search.mock.calls[0][1].aborted).toBe(true);
  fireEvent.submit(input.closest('form')!);
  await act(async () => { resolveFirst({ results: [] }); });
  expect(search).toHaveBeenCalledTimes(2);
  expect(screen.getByRole('button', { name: /Current/ }).querySelector('mark')?.textContent).toBe('我是');
  fireEvent.click(screen.getByRole('button', { name: 'Clear search' }));
  expect(screen.queryByRole('list')).toBeNull();
  expect(document.activeElement).toBe(input);
});

it.each(['empty', 'unavailable'])('retries a search with no displayed results (%s)', async kind => {
  const search = vi.spyOn(api, 'searchChats');
  if (kind === 'empty') search.mockResolvedValueOnce({ results: [] });
  else search.mockRejectedValueOnce(new Error('Session not found.'));
  let finish!: () => void;
  search.mockImplementationOnce(() => new Promise(resolve => {
    finish = () => resolve({ results: [{ title: '[a]', session_id: 's', role: 'title', message_id: '', excerpt: '[a]' }] });
  }));
  const onOpen = vi.fn();
  render(<ChatSearchPanel onOpen={onOpen}>Recent chats</ChatSearchPanel>);
  const input = screen.getByRole('searchbox');
  fireEvent.change(input, { target: { value: '[a]' } });
  fireEvent.submit(input.closest('form')!);
  expect(await screen.findByText('No results to display.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(screen.getByText('Searching…')).toBeTruthy();
  await act(async () => { finish(); });
  const result = screen.getByRole('button', { name: /\[a\]/ });
  expect(result.querySelector('mark')?.textContent).toBe('[a]');
  fireEvent.click(result);
  expect(onOpen).toHaveBeenCalledWith('s');
});
