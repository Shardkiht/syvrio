import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import App from './App';
import * as client from './api/client';
import { api } from './api/client';
import { sessionHistory, switchResponse } from './test/sessionHistory';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('renders cached history immediately, validates its version in the switch, and replaces stale content', async () => {
  const rows = ['a', 'b'].map(id => ({ id, title: id, messages: 1, started_at: '', updated_at: '', current: false }));
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: rows });
  vi.spyOn(api, 'health').mockResolvedValue({ provider: 'test', model: 'model', status: 'ok',
    version: 'test', busy: false, static_ready: true, active_turn_id: null, single_client: false });
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(api, 'contextUsage').mockResolvedValue({ session_id: null, used: 0, limit: 50000, compact_threshold: .8 });
  let bound: string | null = null;
  vi.spyOn(client, 'queueState').mockImplementation(async () => ({ session_id: bound, revision: 0,
    active: null, waiting: [], paused: false }));
  const calls: string[] = [];
  let respond!: (body: unknown) => void;
  vi.stubGlobal('fetch', (path: string, init?: RequestInit) => {
    const route = new URL(path, 'http://localhost');
    calls.push((init?.method ?? 'GET') + ' ' + route.pathname + route.search);
    if (!route.pathname.endsWith('/switch')) throw new Error('Unexpected extra request: ' + path);
    return new Promise<Response>(resolve => {
      respond = body => {
        bound = route.pathname.split('/')[3];
        resolve(new Response(JSON.stringify(body)));
      };
    });
  });
  async function select(id: string, known?: string) {
    const count = calls.length;
    fireEvent.click(await screen.findByTestId('session-' + id));
    expect(screen.getByTestId('session-' + id).getAttribute('aria-current')).toBe('page');
    await waitFor(() => expect(calls).toHaveLength(count + 1));
    expect(calls[count]).toBe('POST /api/sessions/' + id + '/switch?include_history=true'
      + (known ? '&known_version=' + known : ''));
  }
  render(<App />);
  const shortcuts = 'Enter to send or queue · Ctrl+Enter to insert';
  expect(screen.getByText(shortcuts)).toBeTruthy();
  await select('a');
  expect(await screen.findByText('Loading conversation…')).toBeTruthy();
  expect(document.querySelector('.main.is-empty')).toBeNull();
  expect(document.querySelector('.composer-hint')?.textContent).toBe(shortcuts);
  const old = sessionHistory('a', 'Old answer');
  old.items.push({ ...old.items[0], id: 'reply', role: 'assistant', content: 'Answer', reasoning: { text: 'Reasoning details', seconds: 1 } });
  await act(async () => respond(switchResponse(old, 'a1')));
  await screen.findByText('Old answer');
  fireEvent.click(screen.getByRole('button', { name: 'Open commands' }));
  fireEvent.keyDown(screen.getByTestId('palette-input'), { key: 'Escape' });
  expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Message' }));
  const sidebarButton = screen.getByTestId('session-b');
  sidebarButton.focus();
  await select('b');
  expect(document.activeElement).toBe(sidebarButton);
  expect(screen.queryByText('Old answer')).toBeNull();
  await act(async () => respond(switchResponse(sessionHistory('b', 'Another chat'), 'b1')));
  await screen.findByText('Another chat');

  await select('a', 'a1');
  expect(screen.getByText('Old answer')).toBeTruthy();
  expect(screen.queryByText('Loading conversation…')).toBeNull();
  expect(document.querySelector('.composer-hint')?.textContent).toBe(shortcuts);
  fireEvent.click(screen.getByRole('button', { name: /Thought for/ }));
  expect(screen.getByTestId('thinking-body').textContent).toContain('Reasoning details');
  await act(async () => respond({ session_id: 'a', title: 'a', history_version: 'a1', history: null }));
  expect(screen.getByText('Old answer')).toBeTruthy();
  expect(screen.getByTestId('thinking-body').textContent).toContain('Reasoning details');
  await select('b', 'b1');
  await act(async () => respond({ session_id: 'b', title: 'b', history_version: 'b1', history: null }));
  await select('a', 'a1');
  expect(screen.getByText('Old answer')).toBeTruthy();
  await act(async () => respond(switchResponse(sessionHistory('a', 'Updated answer'), 'a2')));
  await screen.findByText('Updated answer');
  expect(screen.queryByText('Old answer')).toBeNull();
  expect(calls).toHaveLength(5);
});

it.each(['confirmed', 'failed', 'superseded'] as const)(
  'keeps a send with its previewed destination when binding is %s', async outcome => {
    const row = (id: string) => ({ id, title: id, messages: 1, started_at: '', updated_at: '', current: false });
    vi.spyOn(api, 'listSessions').mockResolvedValue({ items: ['a', 'b', 'c'].map(row) });
    vi.spyOn(api, 'health').mockResolvedValue({ provider: 'test', model: 'model', status: 'ok',
      version: 'test', busy: false, static_ready: true, active_turn_id: null, single_client: false });
    vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
    const history = vi.spyOn(api, 'history').mockImplementation(async id => sessionHistory(id, 'History ' + id));
    const turn = { session_id: 'a', turn_id: 1, text: 'Running in A' };
    let bound = 'a';
    vi.spyOn(client, 'queueState').mockImplementation(async () => ({ session_id: bound, revision: 0,
      waiting: [], paused: false, active: bound === 'a' ? turn : null }));
    vi.spyOn(client, 'replayTurn').mockImplementation(async (_id, _seq, _emit, signal) => {
      if (!signal?.aborted) await new Promise<void>(resolve => signal?.addEventListener('abort', () => resolve()));
    });
    const cancel = vi.spyOn(api, 'cancelTurn');
    let finish!: () => void;
    const hold = new Promise<void>(resolve => { finish = resolve; });
    let pending = true;
    vi.spyOn(api, 'switchSession').mockImplementation(async id => {
      if (id === 'b' && pending) {
        await hold;
        pending = false;
        if (outcome === 'failed') throw new client.ApiError('Could not switch', 'denied', 409);
      }
      bound = id;
      return switchResponse(sessionHistory(id, 'History ' + id));
    });
    const submit = vi.spyOn(client, 'submitChat').mockResolvedValue({ session_id: 'b', turn_id: 2,
      text: 'B draft', history_only: true });
    const input = () => screen.getByRole('textbox', { name: 'Message' }) as HTMLTextAreaElement;
    const click = async (id: string) => fireEvent.click(await screen.findByTestId('session-' + id));
    render(<App />);
    await click('a');
    await screen.findByRole('button', { name: 'Stop' });
    await click('b');
    expect(screen.getByTestId('session-b').getAttribute('aria-current')).toBe('page');
    expect(screen.queryByText('Loading conversation…')).toBeNull();
    expect(history).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(cancel).not.toHaveBeenCalled();
    const file = new File(['test'], 'b.txt');
    fireEvent.change(input(), { target: { value: 'B draft' } });
    fireEvent.change(screen.getByLabelText('Select attachments'), { target: { files: [file] } });
    // 等待切换期间，文本框仍支持原生换行。
    expect(fireEvent.keyDown(input(), { key: 'Enter', shiftKey: true })).toBe(true);
    expect(input().readOnly).toBe(false);
    fireEvent.keyDown(input(), { key: 'Enter' });
    fireEvent.keyDown(input(), { key: 'Enter' });
    expect(screen.getByText('Waiting to send…')).toBeTruthy();
    expect(submit).not.toHaveBeenCalled();
    if (outcome === 'superseded') await click('c');
    await act(async () => { finish(); });
    if (outcome === 'confirmed') {
      await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
      expect(submit.mock.calls[0][0]).toMatchObject({ session_id: 'b', message: 'B draft' });
      expect(submit.mock.calls[0][1]).toEqual([file]);
      expect(input().value).toBe('');
    } else {
      await screen.findByText(outcome === 'failed' ? 'Could not switch' : 'History c');
      expect(input().value).toBe('');
      await click('b');
      await waitFor(() => expect(input().value).toBe('B draft'));
      expect(screen.getByRole('button', { name: 'Remove b.txt' })).toBeTruthy();
      expect(submit).not.toHaveBeenCalled();
    }
  },
);

it('assigns the original blank draft to a late first receipt and keeps the next blank draft separate', async () => {
  const origin = { id: 'a', title: 'First', messages: 1, started_at: '', updated_at: '', current: false };
  vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [origin] });
  vi.spyOn(api, 'health').mockResolvedValue({ provider: 'test', model: 'model', status: 'ok',
    version: 'test', busy: false, static_ready: true, active_turn_id: null, single_client: false });
  vi.spyOn(api, 'commands').mockResolvedValue({ items: [] });
  vi.spyOn(client, 'queueState').mockResolvedValue({ session_id: null, revision: 0, waiting: [], paused: false, active: null });
  vi.spyOn(api, 'history').mockResolvedValue({ session_id: 'a', title: 'First', current: true, summary: null, has_earlier: false, items: [] });
  vi.spyOn(api, 'switchSession').mockResolvedValue(switchResponse(sessionHistory('a')));
  const create = vi.spyOn(api, 'createSession').mockResolvedValue({ session_id: null, title: '' });
  let receipt!: (value: client.TurnReceipt) => void;
  const submit = vi.spyOn(client, 'submitChat').mockReturnValue(new Promise(resolve => { receipt = resolve; }));
  const input = () => screen.getByRole('textbox', { name: 'Message' }) as HTMLTextAreaElement;
  render(<App />);
  await screen.findByTestId('session-a');
  fireEvent.change(input(), { target: { value: 'First question' } });
  fireEvent.keyDown(input(), { key: 'Enter' });
  await waitFor(() => expect(submit).toHaveBeenCalledOnce());
  fireEvent.change(input(), { target: { value: 'Draft for the original chat' } });
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }));
  expect(input().value).toBe('');
  fireEvent.change(input(), { target: { value: 'Draft for the next chat' } });
  fireEvent.keyDown(input(), { key: 'Enter' });
  expect(create).not.toHaveBeenCalled();
  await act(async () => { receipt({ session_id: 'a', turn_id: 1, text: 'First question' }); });
  await waitFor(() => expect(create).toHaveBeenCalledOnce());
  expect(input().value).toBe('Draft for the next chat');
  fireEvent.click(screen.getByTestId('session-a'));
  await waitFor(() => expect(input().value).toBe('Draft for the original chat'));
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }));
  await waitFor(() => expect(input().value).toBe('Draft for the next chat'));
  expect(submit).toHaveBeenCalledOnce();
});
