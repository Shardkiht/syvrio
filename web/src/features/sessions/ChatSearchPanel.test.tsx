import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { Sidebar } from './Sidebar';

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

function openSearch(onSelect = vi.fn()) {
  render(<Sidebar sessions={[]} currentId={null} replyingSessionIds={new Set()} previewTitle={item => item.title}
    open onNew={vi.fn()} onSelect={onSelect} onArchive={vi.fn()} onOpenArchive={vi.fn()} onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Search chats' }));
  return screen.getByRole('searchbox');
}

it('waits for committed IME input, cancels the previous read and clears the results', async () => {
  vi.useFakeTimers();
  const search = vi.spyOn(api, 'searchChats');
  let resolveFirst!: (value: Awaited<ReturnType<typeof api.searchChats>>) => void;
  search.mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve; }))
    .mockResolvedValue({ results: [{ title: 'Current', session_id: 's', message_id: 'm', role: 'user', excerpt: '我是' }] });
  const input = openSearch();
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

it('keeps the first literal title match separate from the ellipsized context', async () => {
  const title = 'A long session title before the match — 玻璃[A] and 玻璃[a]';
  vi.spyOn(api, 'searchChats').mockResolvedValue({ results: [{
    title, session_id: 's', role: 'user', message_id: 'm', excerpt: '玻璃[a] appears twice: 玻璃[A].',
  }] });
  const input = openSearch();
  fireEvent.change(input, { target: { value: '玻璃[a]' } });
  fireEvent.submit(input.closest('form')!);
  const heading = await screen.findByTitle(title);
  expect(heading.textContent).toBe(title);
  expect(heading.querySelector(':scope > mark')?.textContent).toBe('玻璃[A]');
  expect(Array.from(heading.closest('button')!.querySelectorAll('mark'), mark => mark.textContent))
    .toEqual(['玻璃[A]', '玻璃[a]', '玻璃[a]', '玻璃[A]']);
});

it('dismisses on an outside click without taking focus or leaving a pending search', async () => {
  vi.useFakeTimers();
  const search = vi.spyOn(api, 'searchChats');
  render(<button type="button">Outside</button>);
  const input = openSearch();
  fireEvent.click(input);
  fireEvent.change(input, { target: { value: 'draft' } });
  const outside = screen.getByRole('button', { name: 'Outside' });
  outside.focus();
  fireEvent.click(outside);
  expect(screen.queryByRole('searchbox')).toBeNull();
  expect(document.activeElement).toBe(outside);
  await act(async () => { await vi.advanceTimersByTimeAsync(350); });
  expect(search).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Search chats' }));
  expect(screen.getByRole<HTMLInputElement>('searchbox').value).toBe('');
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }));
  expect(screen.queryByRole('searchbox')).toBeNull();
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
  const input = openSearch(onOpen);
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
