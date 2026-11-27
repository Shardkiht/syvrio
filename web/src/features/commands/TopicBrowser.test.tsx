import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import type { MemoryPage } from '../../types';
import { TopicBrowser } from './TopicBrowser';

const topics = [
  { topic: 'library/reading', title: 'Reading notes', entries: 2 },
  { topic: 'crafts/reading', title: 'Pattern reading', entries: 1 },
  { topic: 'crafts/materials', title: 'Materials', entries: 0 },
];
const reading: MemoryPage = {
  next: null,
  entries: [
    { id: 'mem-sample-a', topic: 'library/reading', revision: 1, text: 'First line.\n  Indented line.' },
    { id: 'mem-sample-b', topic: 'library/reading', revision: 1, text: 'Keep **literal markers**.' },
  ],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe('TopicBrowser', () => {
  it('groups by category, filters by title or path, and reopens the list at the selected row', async () => {
    const readTopics = vi.fn();
    const readTopic = vi.spyOn(api, 'memories').mockReset().mockResolvedValue(reading);
    render(<TopicBrowser initialTopics={topics} readTopics={readTopics} onClose={vi.fn()} />);
    expect(screen.getAllByRole('heading', { level: 3 }).map((item) => item.textContent)).toEqual(['library', 'crafts']);
    expect(screen.getByRole('button', { name: 'Materials 0 entries' })).toBeTruthy();
    const input = screen.getByRole<HTMLInputElement>('searchbox');
    fireEvent.change(input, { target: { value: 'CRAFTS' } });
    expect(screen.queryByRole('button', { name: /Reading notes/ })).toBeNull();
    fireEvent.change(input, { target: { value: 'Reading notes' } });
    const list = screen.getByRole('region', { name: 'Topic list' });
    list.scrollTop = 115;
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    const row = screen.getByRole('button', { name: 'Reading notes 2 entries' });
    expect(document.activeElement).toBe(row);
    fireEvent.click(row);
    expect(readTopic).toHaveBeenCalledWith(topics[0].topic, '', '', expect.any(AbortSignal));
    const back = screen.getByRole('button', { name: 'Back to topics' });
    expect(document.activeElement).toBe(back);
    const entries = await screen.findAllByRole('listitem');
    expect(entries.map((item) => item.querySelector('p')?.textContent)).toEqual(reading.entries.map((entry) => entry.text));
    fireEvent.click(back);
    expect(input.value).toBe('Reading notes');
    expect(list.scrollTop).toBe(115);
    expect(document.activeElement).toBe(row);
    expect(readTopics).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: 'unmatched' } });
    expect(screen.getByText('No matching topics.')).toBeTruthy();
  });

  it('loads the directory only when navigating back from a direct topic command', async () => {
    const catalog = deferred<typeof topics>();
    const readTopics = vi.fn(() => catalog.promise);
    const readTopic = vi.spyOn(api, 'memories').mockReset().mockResolvedValue(reading);
    const close = vi.fn();
    render(<TopicBrowser initialTopic="library/reading" readTopics={readTopics} onClose={close} />);
    expect(screen.getByRole('heading', { name: 'reading' })).toBeTruthy();
    expect(await screen.findByText(reading.entries[1].text)).toBeTruthy();
    expect(readTopics).not.toHaveBeenCalled();
    expect(readTopic).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'Back to topics' }));
    expect(readTopics).toHaveBeenCalledOnce();
    await act(async () => catalog.resolve(topics));
    expect(screen.getByRole('button', { name: /Materials/ })).toBeTruthy();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(close).toHaveBeenCalledOnce();
  });

  it('keeps late content from a previous selection out of the current detail', async () => {
    const first = deferred<MemoryPage>();
    const second = deferred<MemoryPage>();
    vi.spyOn(api, 'memories').mockReset().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<TopicBrowser initialTopics={topics} readTopics={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /Reading notes/ }));
    expect(screen.getByRole('status').textContent).toBe('Loading entries…');
    fireEvent.click(screen.getByRole('button', { name: 'Back to topics' }));
    fireEvent.click(screen.getByRole('button', { name: /Pattern reading/ }));
    await act(async () => first.resolve(reading));
    expect(screen.getByRole('status').textContent).toBe('Loading entries…');
    await act(async () => second.resolve({ next: null, entries: [] }));
    expect(screen.getByText('No entries in this topic.')).toBeTruthy();
    expect(screen.queryByText(reading.entries[1].text)).toBeNull();
  });

  it('retries list and detail failures in place and gives keyboard users a close control', async () => {
    const readTopics = vi.fn().mockRejectedValueOnce(new Error('Temporary outage.')).mockResolvedValueOnce(topics);
    const readTopic = vi.spyOn(api, 'memories').mockReset().mockRejectedValueOnce(new Error('Try again.')).mockResolvedValueOnce(reading);
    const onClose = vi.fn();
    render(<TopicBrowser readTopics={readTopics} onClose={onClose} />);
    expect(screen.getByRole('status').textContent).toBe('Loading topics…');
    expect((await screen.findByRole('alert')).textContent).toContain('Temporary outage.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(document.activeElement).toBe(screen.getByRole('dialog'));
    fireEvent.click(await screen.findByRole('button', { name: /Reading notes/ }));
    expect((await screen.findByRole('alert')).textContent).toContain('Try again.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    const region = screen.getByRole('region', { name: 'Topic entries' });
    expect(await within(region).findByText(reading.entries[1].text)).toBeTruthy();
    expect(readTopic.mock.calls.map(([topic]) => topic)).toEqual([topics[0].topic, topics[0].topic]);
    fireEvent.click(screen.getByRole('button', { name: 'Close topics' }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('searches all memory text and requests only the selected page', async () => {
    const read = vi.spyOn(api, 'memories').mockReset().mockResolvedValue({ ...reading, next: 'cursor' });
    render(<TopicBrowser initialTopic="" readTopics={vi.fn()} onClose={vi.fn()} />);
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'seed' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
    expect(read).toHaveBeenLastCalledWith('', 'seed', 'cursor', expect.any(AbortSignal));
    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    expect(read).toHaveBeenLastCalledWith('', 'seed', '', expect.any(AbortSignal));
    expect(await screen.findByText('Keep **literal markers**.')).toBeTruthy();
  });

  it('keeps a manual edit inline and only writes after confirmation', async () => {
    vi.spyOn(api, 'memories').mockReset().mockResolvedValue(reading);
    const edit = vi.spyOn(api, 'editMemory').mockResolvedValue({ id: reading.entries[0].id });
    render(<TopicBrowser initialTopic="library/reading" readTopics={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click((await screen.findAllByRole('button', { name: 'Edit' }))[0]);
    const input = screen.getByLabelText<HTMLTextAreaElement>('Memory text');
    expect(input.value).toBe(reading.entries[0].text);
    expect(screen.getByRole<HTMLInputElement>('searchbox').disabled).toBe(true);
    fireEvent.change(input, { target: { value: 'Updated note' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(edit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(input.value).toBe('Updated note');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm save' }));
    expect(await screen.findByText('Memory saved.')).toBeTruthy();
    expect(edit).toHaveBeenCalledWith(reading.entries[0].id, 'Updated note', 1);
    expect(screen.getByRole<HTMLInputElement>('searchbox').disabled).toBe(false);
  });

  it('adds a memory from an empty library after reviewing the topic and text', async () => {
    vi.spyOn(api, 'memories').mockReset().mockResolvedValue({ entries: [], next: null });
    const add = vi.spyOn(api, 'addMemory').mockResolvedValue({ id: 'synthetic' });
    render(<TopicBrowser initialTopics={[]} readTopics={vi.fn().mockResolvedValue([])} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'All memories' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add memory' }));
    fireEvent.change(screen.getByLabelText('Topic'), { target: { value: 'notes' } });
    fireEvent.change(screen.getByLabelText('Memory text'), { target: { value: 'Synthetic memory' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(add).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm save' }));
    expect(await screen.findByText('Memory saved.')).toBeTruthy();
    expect(add).toHaveBeenCalledWith('notes', 'Synthetic memory', expect.stringMatching(/^[a-f0-9]{32}$/));
  });

  it('distinguishes an empty catalog from an unloaded catalog', () => {
    const readTopics = vi.fn();
    render(<TopicBrowser initialTopics={[]} readTopics={readTopics} onClose={vi.fn()} />);
    expect(screen.getByText('No topics yet.')).toBeTruthy();
    expect(screen.queryByRole('searchbox')).toBeNull();
    expect(readTopics).not.toHaveBeenCalled();
  });
});
