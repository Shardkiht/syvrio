import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import * as binary from './state/editFiles';
import { loadEdits } from './state/editRecovery';

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const file = { id: 'file', name: 'notes.txt', media_type: 'text/plain', message_id: 'original' };

function fixture(outcome: 'lost' | 'invalid' | 'stale' | 'inflight' | 'not-saved') {
  window.sessionStorage.clear();
  let selected = 'one';
  let accepted = false;
  let newer = false;
  let reads = 0;
  const tokens: (string | null)[] = [];
  const requests: { url: string; session: string; text: string; files: string[] }[] = [];
  vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
    const path = url.split('?')[0];
    if (path.endsWith('/switch')) selected = path.split('/')[3];
    if (path === '/api/sessions') return json({ items: ['one', 'two'].map(id => ({
      id, title: id, current: id === selected, updated_at: '', started_at: '', messages: 1,
    })) });
    if (path === '/api/health') return json({ provider: 'test', model: 'test', static_ready: true });
    if (path === '/api/commands') return json({ items: [] });
    if (path.endsWith('/draft')) return json({ text: 'original question', attachments: [file] });
    if (path.endsWith('/attachments/file')) { reads++; return new Response('retained bytes'); }
    if (path === '/api/sessions/one' || path === '/api/sessions/two' || path.endsWith('/switch')) {
      const id = path.split('/')[3];
      const items = id === 'one' ? [{ id: accepted ? 'new' : 'original', role: 'user',
        content: accepted ? 'replacement answer accepted' : 'original question' }] : [];
      if (id === 'one' && outcome === 'not-saved' && !accepted) {
        items.push({ id: 'original-reply', role: 'assistant', content: 'original reply' });
      }
      if (id === 'one' && newer) items.push({ id: 'newer', role: 'user', content: 'newer question from another view' });
      const history = { session_id: id, title: id, items: items.map(item => ({ ...item, tool_calls: [] })), current: true };
      if (path.endsWith('/switch')) {
        const version = String(Number(accepted) + Number(newer));
        const known = new URL(url, 'http://localhost').searchParams.get('known_version');
        return json({ session_id: id, title: id, history_version: version, history: known === version ? null : history });
      }
      return json(history);
    }
    if (path === '/api/chat/queue') return json({ session_id: selected, active: null, waiting: [], revision: Number(accepted) + Number(newer) });
    if (path === '/api/chat' || path === '/api/chat/attachments') {
      const body = init?.body;
      const data = body instanceof FormData ? {
        session: String(body.get('session_id')), text: String(body.get('message')),
        files: body.getAll('files').map(value => (value as File).name),
      } : { session: JSON.parse(String(body)).session_id, text: JSON.parse(String(body)).message, files: [] };
      tokens.push(new Headers(init?.headers).get('X-Syvrio-Edit'));
      requests.push({ url, ...data });
      if (requests.length === 1 && outcome === 'invalid') return json({ error: { code: 'invalid_upload', message: 'Invalid file' } }, 422);
      if (requests.length === 1 && outcome === 'not-saved') {
        return json({ error: { code: 'edit_not_sent', message: 'The original turn was kept. Retry this edit.' } }, 503);
      }
      if (outcome === 'stale') return json({ error: { code: 'edit_conflict', message: 'Original changed' } }, 409);
      accepted = true;
      if (requests.length === 1 && outcome === 'inflight') return new Promise<Response>(() => {});
      if (requests.length === 1 && outcome === 'lost') throw new TypeError('Connection lost');
      return json({ session_id: 'one', turn_id: 7, text: data.text }, 202);
    }
    return json({});
  });
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL() { return 'blob:retained-file'; }
    static revokeObjectURL() {}
  });
  return { requests, tokens, reads: () => reads, addNewer: () => { newer = true; } };
}

function mockFileStore() {
  const batches = new Map<string, Map<string, File>>();
  vi.spyOn(binary, 'saveEditFiles').mockImplementation(async (id, ids, files) => {
    batches.set(id, new Map(ids.map((key, i) => [key, files[i]])));
  });
  vi.spyOn(binary, 'readEditFiles').mockImplementation(async (id, items) => items.map(i => batches.get(id)!.get(i.id)!));
  vi.spyOn(binary, 'removeEditFiles').mockImplementation(async id => { batches.delete(id); });
  return batches;
}

async function clickResend() {
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit and resend last message' }));
  fireEvent.change(await screen.findByRole('textbox', { name: 'Edit last message' }), { target: { value: 'my revision' } });
  const composer = screen.getByRole('textbox', { name: 'Message' });
  fireEvent.change(composer, { target: { value: 'separate unsent note' } });
  fireEvent.click(screen.getByRole('button', { name: 'Resend' }));
}

async function submitEdit() {
  await clickResend();
  return within(await screen.findByRole('region', { name: 'Saved message edit' }));
}

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('recovering a message edit', () => {
  it('keeps the original exchange and an editable draft with its file after a storage failure and reload', async () => {
    const { requests, tokens } = fixture('not-saved');
    mockFileStore();
    const region = await submitEdit();
    expect(region.getByRole('alert').textContent).toContain('The original turn was kept');
    expect(screen.getByText('original question')).toBeTruthy();
    expect(screen.getByText('original reply')).toBeTruthy();
    cleanup();
    render(<App />);
    const restored = within(await screen.findByRole('region', { name: 'Saved message edit' }));
    await restored.findByRole('link', { name: 'notes.txt' });
    expect(await screen.findByText('original reply')).toBeTruthy();
    const editor = restored.getByRole('textbox');
    expect(editor).toHaveProperty('value', 'my revision');
    expect(editor).toHaveProperty('readOnly', false);
    expect(requests).toHaveLength(1);
    fireEvent.change(editor, { target: { value: 'revised after reload' } });
    fireEvent.click(restored.getByRole('button', { name: 'Retry edit' }));
    await screen.findByText('replacement answer accepted');
    expect(screen.queryByText('original reply')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Saved message edit' })).toBeNull();
    expect(requests[1]).toEqual({ ...requests[0], text: 'revised after reload' });
    expect(tokens[1]).toBe(tokens[0]);
  });

  it('keeps ambiguous submissions and their files in the original session until the receipt is confirmed', async () => {
    const { requests, reads } = fixture('lost');
    await submitEdit();
    await screen.findByText('replacement answer accepted');
    fireEvent.click(screen.getByTestId('session-two'));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Saved message edit' })).toBeNull());
    fireEvent.click(screen.getByTestId('session-one'));
    const region = within(await screen.findByRole('region', { name: 'Saved message edit' }));
    expect(region.getByRole('textbox')).toHaveProperty('readOnly', true);
    expect(region.getByRole('link', { name: 'notes.txt' }).getAttribute('download')).toBe('notes.txt');
    fireEvent.click(region.getByRole('button', { name: 'Retry edit' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Saved message edit' })).toBeNull());
    expect(requests).toHaveLength(2);
    expect(requests[1]).toEqual(requests[0]);
    expect(requests[0]).toEqual({ url: '/api/chat/attachments?receipt=1&replace=original',
      session: 'one', text: 'my revision', files: ['notes.txt'] });
    expect(reads()).toBe(1);
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveProperty('value', 'separate unsent note');
  });

  it('restores an in-flight edit and its files after an offline reload without resubmitting it', async () => {
    const { requests, tokens, reads } = fixture('inflight');
    // jsdom has no IndexedDB. The manifest and component use a separate blob store.
    const batches = mockFileStore();
    await clickResend();
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(loadEdits().one.draft.receiptUnknown).toBe(true);
    expect(loadEdits().one.draft.files).toBeUndefined();
    const gateway = globalThis.fetch;
    cleanup();
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    render(<App />);
    const region = within(await screen.findByRole('region', { name: 'Saved message edit' }));
    await region.findByRole('link', { name: 'notes.txt' });
    expect(region.getByRole('textbox')).toHaveProperty('value', 'my revision');
    expect(region.getByRole('textbox')).toHaveProperty('readOnly', true);
    expect(requests).toHaveLength(1);
    vi.stubGlobal('fetch', gateway);
    fireEvent.click(region.getByRole('button', { name: 'Retry edit' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Saved message edit' })).toBeNull());
    expect(requests).toHaveLength(2);
    expect(requests[1]).toEqual(requests[0]);
    expect(tokens[0]).toMatch(/^[a-f0-9]{32}$/);
    expect(tokens[1]).toBe(tokens[0]);
    expect(reads()).toBe(1);
    expect(batches.size).toBe(0);
    expect(loadEdits()).toEqual({});
  });

  it('preserves an unsent edit and its original file across a reload and a newer turn in another view', async () => {
    const { requests, addNewer } = fixture('lost');
    mockFileStore();
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit and resend last message' }));
    const editor = await screen.findByRole('textbox', { name: 'Edit last message' });
    fireEvent.change(editor, { target: { value: 'never submitted' } });
    cleanup();
    render(<App />);
    expect(await screen.findByRole('textbox', { name: 'Edit last message' })).toHaveProperty('value', 'never submitted');
    await screen.findByRole('link', { name: 'notes.txt' });
    addNewer();
    const saved = within(await screen.findByRole('region', { name: 'Saved message edit' }, { timeout: 3000 }));
    expect(saved.getByRole('textbox')).toHaveProperty('value', 'never submitted');
    expect(saved.getByRole('link').getAttribute('download')).toBe('notes.txt');
    expect(saved.queryByRole('button', { name: 'Resend' })).toBeNull();
    expect(requests).toHaveLength(0);
    cleanup();
    render(<App />);
    const restored = within(await screen.findByRole('region', { name: 'Saved message edit' }));
    expect(restored.getByRole('textbox')).toHaveProperty('value', 'never submitted');
    expect(await restored.findByRole('link', { name: 'notes.txt' })).toBeTruthy();
    expect(requests).toHaveLength(0);
  });

  it('does not recover an untouched or cancelled editor on the next page load', async () => {
    const { requests } = fixture('lost');
    const batches = mockFileStore();
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit and resend last message' }));
    await screen.findByRole('textbox', { name: 'Edit last message' });
    cleanup();
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit and resend last message' }));
    const editor = await screen.findByRole('textbox', { name: 'Edit last message' });
    fireEvent.change(editor, { target: { value: 'cancel me' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(loadEdits()).toEqual({});
    expect(batches.size).toBe(0);
    cleanup();
    render(<App />);
    await screen.findByRole('button', { name: 'Edit and resend last message' });
    expect(screen.queryByRole('textbox', { name: 'Edit last message' })).toBeNull();
    expect(requests).toHaveLength(0);
  });

  it('retains the current draft and explains the limit when browser storage refuses it', async () => {
    fixture('lost');
    const write = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key.startsWith('syvrio.edit.')) throw new Error('Quota exceeded');
      write.call(this, key, value);
    });
    const region = await submitEdit();
    expect(region.getByRole('textbox')).toHaveProperty('value', 'my revision');
    expect(region.getByRole('status').textContent).toContain('Keep this tab open');
    expect(loadEdits()).toEqual({});
  });

  it('removes the stored draft and attachment batch when the edit is discarded', async () => {
    fixture('lost');
    vi.spyOn(binary, 'saveEditFiles').mockResolvedValue(undefined);
    const remove = vi.spyOn(binary, 'removeEditFiles').mockResolvedValue(undefined);
    const region = await submitEdit();
    const id = loadEdits().one.draft.editId;
    fireEvent.click(region.getByRole('button', { name: 'Discard edit' }));
    expect(loadEdits()).toEqual({});
    expect(remove).toHaveBeenCalledWith(id);
  });

  it('accepts corrected text and attachment selection after validation refuses the edit', async () => {
    const { requests } = fixture('invalid');
    const region = await submitEdit();
    expect(screen.getByText('original question')).toBeTruthy();
    fireEvent.change(region.getByRole('textbox'), { target: { value: 'corrected text' } });
    fireEvent.click(region.getByRole('button', { name: 'Remove notes.txt' }));
    fireEvent.click(region.getByRole('button', { name: 'Retry edit' }));
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[1]).toEqual({ url: '/api/chat?receipt=1&replace=original',
      session: 'one', text: 'corrected text', files: [] });
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveProperty('value', 'separate unsent note');
  });

  it('offers the text and actual file for recovery when another client has replaced the target', async () => {
    const { requests } = fixture('stale');
    const region = await submitEdit();
    expect(region.getByRole('alert').textContent).toBe('Original changed');
    expect(region.getByRole('link').getAttribute('href')).toBe('blob:retained-file');
    fireEvent.change(region.getByRole('textbox'), { target: { value: 'recovered text' } });
    expect(region.getByRole('textbox')).toHaveProperty('value', 'recovered text');
    expect(region.queryByRole('button', { name: 'Retry edit' })).toBeNull();
    expect(requests).toHaveLength(1);
  });
});
