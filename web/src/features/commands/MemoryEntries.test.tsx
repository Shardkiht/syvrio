import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { api, ApiError } from '../../api/client';
import type { MemoryRow } from '../../types';
import { MemoryEntries } from './MemoryEntries';

const original: MemoryRow = { id: 'mem-example', topic: 'notes', text: 'Original text', revision: 1 };
const conflict = new ApiError('Memory changed.', 'memory_changed', 409);
const unavailable = new ApiError('Memory unavailable.', 'memory_unavailable', 404);
const button = (name: string) => screen.getByRole<HTMLButtonElement>('button', { name });
afterEach(() => vi.restoreAllMocks());

async function openDraft() {
  const list = vi.spyOn(api, 'memories').mockResolvedValue({ entries: [original], next: null });
  const view = render(<MemoryEntries topic="notes" onChanged={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
  const input = screen.getByLabelText<HTMLTextAreaElement>('Memory text');
  fireEvent.change(input, { target: { value: 'My draft' } });
  return { input, list, view };
}

function confirmSave() {
  fireEvent.click(button('Save'));
  fireEvent.click(button('Confirm save'));
}

it('preserves the draft at each conflict and writes only after reviewing the latest revision again', async () => {
  const edit = vi.spyOn(api, 'editMemory').mockRejectedValueOnce(conflict).mockRejectedValueOnce(conflict)
    .mockResolvedValueOnce({ id: original.id });
  const read = vi.spyOn(api, 'memory')
    .mockResolvedValueOnce({ ...original, text: 'Second text', revision: 2 })
    .mockResolvedValueOnce({ ...original, text: 'Third text', revision: 3 });
  const { input } = await openDraft();
  expect(read).not.toHaveBeenCalled();
  confirmSave();
  expect(await screen.findByText('Second text')).toBeTruthy();
  expect(input.value).toBe('My draft');
  expect(edit).toHaveBeenCalledTimes(1);
  fireEvent.change(input, { target: { value: 'Merged draft' } });
  confirmSave();
  expect(await screen.findByText('Third text')).toBeTruthy();
  expect(input.value).toBe('Merged draft');
  expect(edit).toHaveBeenCalledTimes(2);
  expect(screen.queryByText('Second text')).toBeNull();
  fireEvent.click(button('Save'));
  fireEvent.click(button('Back'));
  expect(edit).toHaveBeenCalledTimes(2);
  confirmSave();
  expect(await screen.findByText('Memory saved.')).toBeTruthy();
  expect(edit.mock.calls).toEqual([
    [original.id, 'My draft', 1], [original.id, 'Merged draft', 2], [original.id, 'Merged draft', 3],
  ]);
});

it('lets the user retry the read without writing, and keeps a copyable draft if the target disappears', async () => {
  const edit = vi.spyOn(api, 'editMemory').mockRejectedValue(conflict);
  const read = vi.spyOn(api, 'memory').mockRejectedValueOnce(new Error('Network unavailable'))
    .mockRejectedValueOnce(unavailable);
  const { input, list } = await openDraft();
  confirmSave();
  fireEvent.click(await screen.findByRole('button', { name: 'Retry loading' }));
  expect(await screen.findByText(/no longer available to edit/)).toBeTruthy();
  expect(button('Save').disabled).toBe(true);
  expect(input.value).toBe('My draft');
  expect(read).toHaveBeenCalledTimes(2);
  expect(edit).toHaveBeenCalledOnce();
  list.mockResolvedValue({ entries: [], next: null });
  fireEvent.click(button('Cancel'));
  expect(await screen.findByText('No entries in this topic.')).toBeTruthy();
});

it('recognizes an identical edit already saved elsewhere without creating another revision', async () => {
  const edit = vi.spyOn(api, 'editMemory').mockRejectedValue(conflict);
  vi.spyOn(api, 'memory').mockResolvedValue({ ...original, text: 'My draft', revision: 2 });
  await openDraft();
  confirmSave();
  expect(await screen.findByText(/draft already matches saved revision 2/)).toBeTruthy();
  expect(button('Save').disabled).toBe(true);
  expect(edit).toHaveBeenCalledOnce();
});

it('blocks a stale save during refresh and cancels the read when its editor closes', async () => {
  vi.spyOn(api, 'editMemory').mockRejectedValue(conflict);
  let finish!: (row: MemoryRow) => void;
  const read = vi.spyOn(api, 'memory').mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const { input } = await openDraft();
  confirmSave();
  expect(await screen.findByText(/Loading the latest text/)).toBeTruthy();
  expect(button('Save').disabled).toBe(true);
  expect(input.value).toBe('My draft');
  const signal = read.mock.calls[0][1];
  fireEvent.click(button('Cancel'));
  expect(signal.aborted).toBe(true);
  await act(async () => finish({ ...original, text: 'Obsolete response', revision: 2 }));
  expect(screen.queryByText('Obsolete response')).toBeNull();
  expect(screen.queryByLabelText('Memory text')).toBeNull();
});
