import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { Markdown } from './components/Markdown';
import type { CommandInfo, CommandResponse, HistoryResponse, StreamEvent } from './types';

const encoder = new TextEncoder();

const HEALTH = {
  status: 'ok', version: '0.4.0.dev0', provider: 'deepseek', model: 'deepseek-flash',
  busy: false, active_turn_id: null, single_client: true, static_ready: true,
};
const SESSIONS = [{
  id: 's-1', title: 'Earlier chat', messages: 1,
  started_at: '', updated_at: '', current: false,
}];

const command = (name: string, patch: Partial<CommandInfo> = {}): CommandInfo => ({
  name,
  usage: `/${name}`,
  description: `${name} description`,
  support: 'server',
  intent: name,
  confirm: false,
  note: '',
  shortcut: '',
  subcommands: [],
  ...patch,
});

const CATALOG: CommandInfo[] = [
  command('new'),
  command('sessions', { support: 'client', intent: 'open_sessions' }),
  command('model', {
    usage: '/model [refresh]',
    subcommands: [{ name: 'refresh', description: 'Refresh model list' }],
  }),
  command('undo'),
  command('import', { support: 'unsupported', note: 'Use the TUI.' }),
  command('help', { shortcut: 'Ctrl+Shift+P / Alt+P' }),
  command('status'),
  command('pending'),
];

const commandResult = (patch: Partial<CommandResponse> = {}): CommandResponse => ({
  status: 'ok',
  command: 'status',
  intent: 'status',
  message: 'ok',
  data: null,
  session_id: null,
  refresh_sessions: false,
  refresh_history: false,
  ...patch,
});

const frame = (event: StreamEvent) =>
  `id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;

function json(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as unknown as Response;
}

function sse(...chunks: string[]): Response {
  let index = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () =>
          index < chunks.length
            ? { done: false, value: encoder.encode(chunks[index++]) }
            : { done: true, value: undefined },
        releaseLock: () => {},
      }),
    },
  } as unknown as Response;
}

function installFetch(overrides: Record<string, (init?: RequestInit) => Response> = {}) {
  const calls: string[] = [];
  vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    calls.push(`${method} ${url}`);
    const key = url.split('?')[0];
    if (overrides[`${method} ${key}`]) return overrides[`${method} ${key}`](init);
    if (overrides[key]) return overrides[key](init);
    if (key === '/api/health') return json(HEALTH);
    if (key === '/api/sessions') return json({ items: SESSIONS });
    if (key === '/api/commands') {
      if (method === 'POST') return json(commandResult());
      return json({ items: CATALOG });
    }
    return json({ error: { code: 'not_found', message: 'Not found' } }, 404);
  });
  return calls;
}

/** Capture POST /api/commands while keeping GET /api/commands serving the catalog. */
function spyCommands(
  respond: (body: { line: string; confirmed: boolean }) => CommandResponse,
) {
  const lines: string[] = [];
  const confirmed: boolean[] = [];
  const overrides: Record<string, (init?: RequestInit) => Response> = {
    '/api/commands': (init) => {
      if (init?.method !== 'POST') return json({ items: CATALOG });
      const body = JSON.parse(String(init.body ?? '{}')) as { line: string; confirmed: boolean };
      lines.push(body.line);
      confirmed.push(body.confirmed);
      return json(respond(body));
    },
  };
  return { lines, confirmed, overrides };
}

const ready = () =>
  waitFor(() => expect(screen.getByTestId('model-info').textContent).toContain('deepseek'));

const send = (text: string) => {
  fireEvent.change(screen.getByTestId('composer-input'), { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
};

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 1024 });
});

describe('Web client interactions', () => {
  it('shows context usage next to Send and updates it when a session opens', async () => {
    const current = SESSIONS[0];
    const seen: (string | null)[] = [];
    vi.stubGlobal('fetch', async (url: string) => {
      const route = new URL(url, 'http://localhost');
      switch (route.pathname) {
        case '/api/health': return json(HEALTH);
        case '/api/sessions': return json({ items: SESSIONS });
        case '/api/commands': return json({ items: CATALOG });
        case '/api/context-usage': {
          const id = route.searchParams.get('session_id');
          seen.push(id);
          return json({ session_id: id, used: id ? 2400 : 0, limit: 4000, compact_threshold: .8 });
        }
        case '/api/sessions/s-1/switch': return json({ session_id: current.id, title: current.title });
        case '/api/sessions/s-1': return json({ session_id: current.id, items: [], title: current.title });
        default: return json({}, 404);
      }
    });
    render(<App />);
    const empty = await screen.findByRole('button', { name: /Context estimate: 0 \/ 4,000 tokens/ });
    expect(empty.textContent).toBe('0%');
    expect(empty.getAttribute('data-wave')).toBe('still');
    expect(empty.parentElement?.nextElementSibling).toBe(screen.getByRole('button', { name: 'Send' }));
    fireEvent.click(screen.getByTestId('session-s-1'));
    const dial = await screen.findByRole('button', { name: /Context estimate: 2,400 \/ 4,000 tokens/ });
    expect(dial.textContent).toBe('60%');
    expect(dial.getAttribute('data-wave')).toBe('moving');
    fireEvent.click(dial);
    expect(dial.getAttribute('aria-expanded')).toBe('true');
    expect(seen).toEqual([null, current.id]);
  });

  it('sends a message and streams the markdown reply', async () => {
    installFetch({
      '/api/chat': () => sse(
        frame({ seq: 1, type: 'start', turn_id: 7, session_id: 's-2' }),
        frame({ seq: 2, type: 'content_delta', text: 'Hello **world**' }),
        frame({ seq: 3, type: 'done', text: 'Hello **world**', turn_id: 7, session_id: 's-2' }),
      ),
    });
    render(<App />);
    await ready();
    send('hi');
    await waitFor(() => expect(screen.getByTestId('user-message').textContent).toContain('hi'));
    await waitFor(() => expect(screen.getByTestId('assistant-message').textContent).toContain('Hello'));
    expect(screen.getByTestId('assistant-message').querySelector('strong')?.textContent).toBe('world');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy());
  });

  it('moves the saved session to the top while its reply is still running', async () => {
    const old = { ...SESSIONS[0], current: true };
    const recent = { ...SESSIONS[0], id: 's-2', title: 'More recent' };
    let saved = false;
    let releaseDone: ((data: string) => void) | undefined;
    const stream = {
      ok: true,
      body: {
        getReader: () => ({
          read: async () => {
            if (!saved) {
              saved = true;
              return {
                done: false,
                value: encoder.encode(frame({ seq: 1, type: 'message_saved', session_id: old.id })),
              };
            }
            if (releaseDone) return { done: true };
            const chunk = await new Promise<string>((resolve) => { releaseDone = resolve; });
            return { done: false, value: encoder.encode(chunk) };
          },
          releaseLock: () => {},
        }),
      },
    } as unknown as Response;
    installFetch({
      '/api/sessions': () => json({ items: saved ? [old, recent] : [recent, old] }),
      '/api/sessions/s-1': () => json({
        session_id: old.id, items: [], title: old.title, current: true,
      }),
      '/api/chat': () => stream,
    });
    render(<App />);
    await ready();
    const sidebar = within(screen.getByTestId('sidebar'));
    await waitFor(() => expect(sidebar.getAllByTestId(/^session-/)[0].dataset.testid).toBe('session-s-2'));
    send('Continue the older chat');
    await waitFor(() => expect(sidebar.getAllByTestId(/^session-/)[0].dataset.testid).toBe('session-s-1'));
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy();
    releaseDone?.(frame({ seq: 2, type: 'done', text: 'Finished', session_id: old.id }));
    await screen.findByRole('button', { name: 'Send' });
  });

  it('collapses thinking and expands it into a bounded pane', async () => {
    installFetch({
      '/api/chat': () => sse(
        frame({ seq: 1, type: 'start', turn_id: 8 }),
        frame({ seq: 2, type: 'reasoning_delta', text: 'line one\nline two\nline three', phase: 1 }),
        frame({ seq: 3, type: 'reasoning_end', text: 'line one\nline two\nline three', seconds: 1.5, phase: 1 }),
        frame({ seq: 4, type: 'content_delta', text: 'answer' }),
        frame({ seq: 5, type: 'done', text: 'answer' }),
      ),
    });
    render(<App />);
    await ready();
    send('hi');
    await waitFor(() => expect(screen.getByTestId('thinking')).toBeTruthy());
    expect(screen.queryByTestId('thinking-body')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Expand/ }));
    expect(screen.getByTestId('thinking-body').textContent).toContain('line one');
    expect(screen.getByTestId('thinking').className).toContain('thinking-expanded');
    fireEvent.click(screen.getByRole('button', { name: /Collapse/ }));
    expect(screen.queryByTestId('thinking-body')).toBeNull();
  });

  it('folds a tool call and reveals its result on demand', async () => {
    installFetch({
      '/api/chat': () => sse(
        frame({ seq: 1, type: 'start', turn_id: 9 }),
        frame({ seq: 2, type: 'tool_start', name: 'recall' }),
        frame({ seq: 3, type: 'tool_end', name: 'recall', text: '1 fact' }),
        frame({ seq: 4, type: 'content_delta', text: 'found it' }),
        frame({ seq: 5, type: 'done', text: 'found it' }),
      ),
    });
    render(<App />);
    await ready();
    send('recall something');
    await waitFor(() => expect(screen.getByTestId('tool-event').textContent).toContain('recall'));
    // The summary row carries name and state; the result stays collapsed.
    expect(screen.getByTestId('tool-event').textContent).toContain('Done');
    expect(screen.getByTestId('tool-event').textContent).not.toContain('1 fact');
    expect(screen.queryByTestId('tool-body')).toBeNull();
    fireEvent.click(within(screen.getByTestId('tool-event')).getByRole('button'));
    expect(screen.getByTestId('tool-body').textContent).toContain('1 fact');
    fireEvent.click(within(screen.getByTestId('tool-event')).getByRole('button'));
    expect(screen.queryByTestId('tool-body')).toBeNull();
  });

  it('folds back-to-back tool calls into one collapsed group', async () => {
    installFetch({
      '/api/chat': () => sse(
        frame({ seq: 1, type: 'start', turn_id: 10 }),
        frame({ seq: 2, type: 'tool_start', name: 'recall' }),
        frame({ seq: 3, type: 'tool_end', name: 'recall', text: 'one fact' }),
        frame({ seq: 4, type: 'tool_start', name: 'search' }),
        frame({ seq: 5, type: 'tool_end', name: 'search', text: 'two hits' }),
        frame({ seq: 6, type: 'content_delta', text: 'done' }),
        frame({ seq: 7, type: 'done', text: 'done' }),
      ),
    });
    render(<App />);
    await ready();
    send('look around');
    await waitFor(() => expect(screen.getByTestId('tool-group')).toBeTruthy());
    const group = screen.getByTestId('tool-group');
    expect(group.textContent).toContain('2 tool calls');
    expect(group.textContent).not.toContain('one fact');
    expect(screen.queryByTestId('tool-group-body')).toBeNull();
    fireEvent.click(within(group).getByRole('button'));
    const body = screen.getByTestId('tool-group-body');
    expect(body.textContent).toContain('recall');
    expect(body.textContent).toContain('one fact');
    expect(body.textContent).toContain('search');
    expect(body.textContent).toContain('two hits');
  });

  it('stops generation by cancelling the active turn', async () => {
    let resolveCancelled: ((chunk: string) => void) | null = null;
    let terminated = false;
    const queued = [frame({ seq: 1, type: 'start', turn_id: 11 })];
    let cancelCalls = 0;
    const stream: Response = {
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: async () => {
            if (queued.length) return { done: false, value: encoder.encode(queued.shift()!) };
            if (terminated) return { done: true, value: undefined };
            const chunk = await new Promise<string>((resolve) => { resolveCancelled = resolve; });
            terminated = true;
            return { done: false, value: encoder.encode(chunk) };
          },
          releaseLock: () => {},
        }),
      },
    } as unknown as Response;

    const calls = installFetch({
      '/api/chat': () => stream,
      '/api/chat/cancel': () => {
        cancelCalls += 1;
        resolveCancelled?.(frame({ seq: 2, type: 'cancelled', turn_id: 11 }));
        return json({ cancelled: true, turn_id: 11, active_turn_id: 11 });
      },
    });
    render(<App />);
    await ready();
    send('long answer');
    await waitFor(() => expect(resolveCancelled).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    await waitFor(() => expect(calls.some((url) => url.includes('/api/chat/cancel'))).toBe(true));
    expect(cancelCalls).toBe(1);
    await waitFor(() => expect(screen.getByTestId('assistant-message').textContent).toContain('Stopped'));
  });

  it('queues ordinary sends in order and sends Ctrl+Enter to the running turn', async () => {
    const sent: string[] = [];
    const inserted: unknown[] = [];
    let finish!: (value: string) => void;
    const streamTail = new Promise<string>((resolve) => { finish = resolve; });
    let readCount = 0;
    const first = {
      ok: true, status: 200,
      body: { getReader: () => ({
        read: async () => {
          if (++readCount === 1) return {
            done: false, value: encoder.encode(frame({ seq: 1, type: 'start', turn_id: 71, session_id: 's-1' })),
          };
          if (readCount === 2) return { done: false, value: encoder.encode(await streamTail) };
          return { done: true };
        },
        releaseLock: () => {},
      }) },
    } as unknown as Response;
    installFetch({
      '/api/chat': (init) => {
        sent.push(JSON.parse(String(init?.body)).message);
        return sent.length === 1 ? first : sse(frame({ seq: 1, type: 'done', text: 'Queued reply' }));
      },
      '/api/chat/insert': (init) => {
        inserted.push(JSON.parse(String(init?.body)));
        return json(null, 204);
      },
    });
    render(<App />);
    await ready();
    send('original');
    await waitFor(() => expect(readCount).toBe(2));
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    for (const text of ['first queued', 'second queued']) {
      fireEvent.change(input, { target: { value: text } });
      fireEvent.keyDown(input, { key: 'Enter' });
    }
    expect(sent).toEqual(['original']);
    expect(screen.getByRole('region', { name: 'Send queue' }).textContent).toContain('2 queued');

    fireEvent.change(input, { target: { value: 'new instruction' } });
    fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true });
    await waitFor(() => expect(inserted).toEqual([
      { turn_id: 71, session_id: 's-1', message: 'new instruction' },
    ]));
    await waitFor(() => expect(input.value).toBe(''));
    expect(sent).toEqual(['original']);
    finish(frame({ seq: 2, type: 'content_delta', text: 'Original response' })
      + frame({ seq: 3, type: 'message_inserted', session_id: 's-1', text: 'new instruction' })
      + frame({ seq: 4, type: 'done', session_id: 's-1', text: 'Supplement response' }));
    await waitFor(() => expect(sent).toEqual(['original', 'first queued', 'second queued']));
    expect(screen.getByTestId('message-list').textContent).toContain('Original response');
    expect(screen.getByTestId('message-list').textContent).toContain('Supplement response');
  });

  it('restores the undone draft and pauses queued messages until the next explicit send', async () => {
    const sent: string[] = [];
    let finish!: (value: string) => void;
    const streamTail = new Promise<string>((resolve) => { finish = resolve; });
    let readCount = 0;
    const first = {
      ok: true, status: 200,
      body: { getReader: () => ({
        read: async () => {
          if (++readCount === 1) return {
            done: false, value: encoder.encode(frame({ seq: 1, type: 'start', turn_id: 72, session_id: 's-1' })),
          };
          if (readCount === 2) return { done: false, value: encoder.encode(await streamTail) };
          return { done: true };
        },
        releaseLock: () => {},
      }) },
    } as unknown as Response;
    installFetch({
      '/api/chat': (init) => {
        sent.push(JSON.parse(String(init?.body)).message);
        return sent.length === 1 ? first : sse(frame({ seq: 1, type: 'done', text: 'Reply' }));
      },
      'POST /api/commands': () => {
        finish(frame({ seq: 2, type: 'cancelled', session_id: 's-1' }));
        return json(commandResult({
          command: 'undo', intent: 'undo', session_id: 's-1',
          data: { restored: 'original question' }, refresh_history: true,
        }));
      },
      '/api/sessions/s-1': () => json({
        session_id: 's-1', title: 'Session', current: true, has_earlier: false, summary: null, items: [],
      }),
    });
    render(<App />);
    await ready();
    send('original question');
    await waitFor(() => expect(readCount).toBe(2));
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'queued question' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    await waitFor(() => expect(input.value).toBe('original question'));
    expect(screen.getByRole('region', { name: 'Send queue' }).textContent).toContain('paused');
    expect(sent).toEqual(['original question']);
    fireEvent.change(input, { target: { value: 'edited question' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(sent).toEqual(['original question', 'edited question', 'queued question']));
  });

  it('restores an existing session from the sidebar', async () => {
    const history: HistoryResponse = {
      session_id: 's-1', title: 'Earlier chat', current: true, has_earlier: false, summary: null,
      items: [
        { id: 'u1', role: 'user', content: 'earlier question', at: '', reasoning: null,
          tool_calls: [], tool_name: null, tool_ok: null, interrupted: false },
        { id: 'a1', role: 'assistant', content: 'earlier answer', at: '', reasoning: null,
          tool_calls: [], tool_name: null, tool_ok: null, interrupted: false },
      ],
    };
    installFetch({
      '/api/sessions/s-1/switch': () => json({ session_id: 's-1', title: 'Earlier chat' }),
      '/api/sessions/s-1': () => json(history),
    });
    render(<App />);
    await ready();
    fireEvent.click(screen.getByTestId('session-s-1'));
    await waitFor(() => expect(screen.getByTestId('message-list').textContent).toContain('earlier question'));
    expect(screen.getByTestId('message-list').textContent).toContain('earlier answer');
  });

  it('shows a browser-safe error when the request fails', async () => {
    installFetch({
      '/api/chat': () => json({ error: { code: 'model_error', message: 'Model unavailable' } }, 500),
    });
    render(<App />);
    await ready();
    send('hi');
    await waitFor(() => expect(screen.getByTestId('error-banner').textContent).toContain('Model unavailable'));
    // A 500 means the server is reachable; only a transport failure is offline.
    expect(screen.getByTestId('connection').textContent).toContain('Connected');
  });

  it('marks the connection offline when the transport fails', async () => {
    installFetch({
      '/api/chat': () => { throw new TypeError('Failed to fetch'); },
    });
    render(<App />);
    await ready();
    send('hi');
    await waitFor(() => expect(screen.getByTestId('error-banner').textContent).toContain('Failed to fetch'));
    expect(screen.getByTestId('connection').textContent).toContain('Offline');
  });

  it('keeps the sidebar off-screen on mobile until requested', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 375 });
    installFetch();
    render(<App />);
    await ready();
    expect(screen.getByTestId('sidebar').className).not.toContain('open');
    fireEvent.click(screen.getByRole('button', { name: 'Open sessions' }));
    expect(screen.getByTestId('sidebar').className).toContain('open');
    expect(screen.getByTestId('sidebar-scrim')).toBeTruthy();
  });

  it('collapses and reopens the desktop sidebar with the visible controls', async () => {
    installFetch();
    render(<App />);
    await ready();

    fireEvent.click(screen.getByRole('button', { name: 'Close sessions' }));
    await waitFor(() => expect(document.querySelector('.app')?.className).toContain('sidebar-collapsed'));
    expect(screen.getByTestId('sidebar').className).not.toContain('open');
    await waitFor(() => expect(window.localStorage.getItem('syvrio.sidebar')).toBe('closed'));

    fireEvent.click(screen.getByRole('button', { name: 'Open sessions' }));
    await waitFor(() => expect(document.querySelector('.app')?.className).not.toContain('sidebar-collapsed'));
    expect(screen.getByTestId('sidebar').className).toContain('open');
  });

  it('folds the sidebar when a desktop window crosses into phone width', async () => {
    installFetch();
    render(<App />);
    await ready();
    expect(screen.getByTestId('sidebar').className).toContain('open');

    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 375 });
    fireEvent(window, new Event('resize'));
    await waitFor(() => expect(screen.getByTestId('sidebar').className).not.toContain('open'));
    expect(screen.queryByTestId('sidebar-scrim')).toBeNull();

    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 1024 });
    fireEvent(window, new Event('resize'));
    await waitFor(() => expect(screen.getByTestId('sidebar').className).toContain('open'));
  });
});

describe('Slash commands and shortcuts', () => {
  it('selects a pending candidate once, edits it, and refreshes after confirmed writes', async () => {
    const first = { id: 'p-1', text: 'First candidate\nSecond line', topic: 'food', reason: 'Check the wording' };
    const second = { id: 'p-2', text: 'Another candidate', topic: 'food', reason: '' };
    const requests: { line: string; confirmed: boolean; pending_text?: string }[] = [];
    let fail = true;
    installFetch({
      'POST /api/commands': (init) => {
        const body = JSON.parse(String(init?.body)) as (typeof requests)[number];
        requests.push(body);
        const pending = (rows: (typeof first)[]) =>
          commandResult({ command: 'pending', intent: 'pending', data: { kind: 'pending', rows, count: rows.length } });
        if (body.line === '/pending') return json(pending([first, second]));
        if (body.line === '/pending edit p-1' && fail) {
          fail = false;
          return json(commandResult({ command: 'pending', status: 'error', message: 'Write failed' }));
        }
        return json(pending(body.line === '/pending edit p-1' ? [second] : []));
      },
    });
    render(<App />);
    await ready();
    send('/pending');
    const dialog = within(await screen.findByRole('dialog', { name: 'Pending review' }));
    const list = within(dialog.getByRole('group', { name: 'Candidates' }));
    expect(list.getAllByRole('button')).toHaveLength(2);
    expect(dialog.queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(dialog.queryByRole('button', { name: 'Reject' })).toBeNull();
    fireEvent.click(list.getByRole('button', { name: /First candidate/ }));
    expect(dialog.getByText('Check the wording')).toBeTruthy();
    fireEvent.click(dialog.getByRole('button', { name: 'Edit' }));
    const editor = dialog.getByRole('textbox', { name: 'Edit candidate' }) as HTMLTextAreaElement;
    expect(editor.value).toBe(first.text);
    fireEvent.change(editor, { target: { value: 'Revised preference\nWith a note' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Save and approve' }));
    expect(requests).toHaveLength(1);
    fireEvent.click(dialog.getByRole('button', { name: 'Confirm' }));
    expect((await dialog.findByRole('alert')).textContent).toBe('Write failed');
    expect(editor.value).toBe('Revised preference\nWith a note');
    fireEvent.click(dialog.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(list.getAllByRole('button')).toHaveLength(1));
    expect(requests[2]).toMatchObject({
      line: '/pending edit p-1', confirmed: true, pending_text: 'Revised preference\nWith a note',
    });
    fireEvent.click(list.getByRole('button', { name: /Another candidate/ }));
    fireEvent.click(dialog.getByRole('button', { name: 'Reject' }));
    fireEvent.click(dialog.getByRole('button', { name: 'Confirm' }));
    expect(await dialog.findByText('No pending candidates')).toBeTruthy();
    expect(requests[3]).toMatchObject({ line: '/pending reject p-2', confirmed: true });
  });

  it('edits an inference with the TUI keys and keeps the response in review', async () => {
    const text = 'Full inference text used as the edit draft.';
    const requests: { line: string; confirmed: boolean; inference_text?: string }[] = [];
    installFetch({
      'POST /api/commands': (init) => {
        const body = JSON.parse(String(init?.body)) as (typeof requests)[number];
        requests.push(body);
        const rows = body.inference_text ? [] : [{
          id: 'inf-example', topic: 'profile', reason: 'hypothesis', text,
        }];
        return json(commandResult({
          command: 'inferences', intent: 'inferences',
          data: { kind: 'inferences', count: rows.length, rows },
        }));
      },
    });
    render(<App />);
    await ready();
    send('/inferences');
    const surface = await screen.findByRole('dialog', { name: 'Inference review' });
    const dialog = within(surface);
    fireEvent.keyDown(surface, { key: 'ArrowRight' });
    expect(dialog.getByRole('button', { name: 'Pending' }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.keyDown(surface, { key: 'ArrowDown' });
    expect(dialog.getByRole('button', { name: /Full inference/ }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.keyDown(document.activeElement!, { key: 'e' });
    const input = dialog.getByRole('textbox', { name: 'Edit inference' }) as HTMLTextAreaElement;
    expect(input.value).toBe(text);
    fireEvent.change(input, { target: { value: 'Edited\nwith a second line' } });
    fireEvent.keyDown(input, { key: 's', ctrlKey: true });
    expect(requests).toHaveLength(1);
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(document.activeElement).toBe(input);
    fireEvent.keyDown(input, { key: 's', ctrlKey: true });
    fireEvent.click(dialog.getByRole('button', { name: 'Confirm' }));
    expect(await dialog.findByText('No inferences')).toBeTruthy();
    expect(requests[1]).toEqual({
      line: '/inferences correct inf-example', confirmed: true,
      inference_text: 'Edited\nwith a second line', session_id: null,
    });
    expect(screen.getByTestId('message-list').textContent).not.toContain('Edited');
  });

  it('opens /sessions in a modal with keyboard navigation and /help in the palette', async () => {
    const spy = spyCommands((body) => body.line === '/sessions'
      ? commandResult({ status: 'client', command: 'sessions', intent: 'open_sessions' })
      : commandResult({ command: 'help', intent: 'help' }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    send('/sessions');
    const dialog = await screen.findByRole('dialog', { name: 'Sessions' });
    fireEvent.keyDown(dialog, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Earlier chat' }));
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    send('/help');
    expect(await screen.findByRole('dialog', { name: 'Command palette' })).toBeTruthy();
  });

  it('opens the Git sync confirmation from its dialog with Ctrl+S', async () => {
    const spy = spyCommands((body) => body.line === '/git'
      ? commandResult({ command: 'git', intent: 'git_status', message: 'modified: 1' })
      : commandResult({ command: 'git', intent: 'git_status', status: 'confirm_required', message: 'Confirm sync' }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    send('/git');
    const dialog = await screen.findByRole('dialog', { name: '/git' });
    fireEvent.keyDown(dialog, { key: 's', ctrlKey: true });
    expect(await screen.findByRole('alertdialog', { name: 'Confirm' })).toBeTruthy();
    expect(spy.lines).toEqual(['/git', '/git sync']);
    expect(spy.confirmed).toEqual([false, false]);
  });

  it('completes by prefix, Tab and Enter without ever calling /api/chat', async () => {
    const spy = spyCommands((body) =>
      commandResult({ command: 'model', intent: 'model', message: `ran ${body.line}` }));
    const calls = installFetch(spy.overrides);
    render(<App />);
    await ready();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/mod' } });
    expect(screen.getByTestId('command-suggestions').textContent).toContain('/model');
    fireEvent.keyDown(input, { key: 'Tab' });
    expect(input.value).toBe('/model ');
    expect(screen.getByTestId('command-suggestions').textContent).toContain('/model refresh');
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(spy.lines).toEqual(['/model refresh']));
    const dialog = await screen.findByRole('dialog', { name: '/model' });
    expect(dialog.textContent).toContain('ran /model refresh');
    expect(screen.getByTestId('message-list').textContent).not.toContain('ran /model refresh');
    expect(calls.some((entry) => entry.includes('/api/chat'))).toBe(false);
    expect(input.value).toBe('');
  });

  it('selects suggestions with arrows and closes them with Escape', async () => {
    const spy = spyCommands(() => commandResult());
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/' } });
    const suggestions = screen.getByTestId('command-suggestions');
    expect(suggestions.querySelectorAll('[role="option"]')).toHaveLength(CATALOG.length);
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(suggestions.querySelector('[aria-selected="true"]')?.textContent).toContain('/sessions');
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(suggestions.querySelector('[aria-selected="true"]')?.textContent).toContain('/new');
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByTestId('command-suggestions')).toBeNull();
    expect(spy.lines).toEqual([]);
  });

  it('reports unknown commands explicitly instead of forwarding them to the LLM', async () => {
    const spy = spyCommands(() => commandResult({
      status: 'unknown', command: 'nope', message: 'Unknown command: /nope.',
    }));
    const calls = installFetch(spy.overrides);
    render(<App />);
    await ready();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/nope' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(spy.lines).toEqual(['/nope']));
    expect(await screen.findByRole('dialog', { name: 'Unknown command' })).toBeTruthy();
    expect(screen.getByTestId('message-list').textContent).not.toContain('Unknown command');
    expect(calls.some((entry) => entry.includes('/api/chat'))).toBe(false);
  });

  it('shows a clear unsupported message for browser-unsafe commands', async () => {
    const spy = spyCommands(() => commandResult({
      status: 'unsupported', command: 'import', intent: '', message: 'Use the TUI.',
    }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/import /tmp/private.txt' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(await screen.findByRole('dialog', { name: '/import is not available on the Web' })).toBeTruthy();
  });

  it('does not send or trigger shortcuts during IME composition', async () => {
    const spy = spyCommands(() => commandResult({ message: 'sent' }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/help' } });
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229, isComposing: true });
    expect(spy.lines).toEqual([]);
    fireEvent.keyDown(window, {
      key: 'p', ctrlKey: true, shiftKey: true, keyCode: 229, isComposing: true,
    });
    expect(screen.queryByTestId('command-palette-overlay')).toBeNull();
  });

  it('leaves Ctrl+P to the browser and offers palette / model / sidebar alternatives', async () => {
    const spy = spyCommands(() => commandResult({ command: 'model', message: 'Model list' }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    fireEvent.keyDown(window, { key: 'p', ctrlKey: true });
    expect(screen.queryByTestId('command-palette-overlay')).toBeNull();
    expect(spy.lines).toEqual([]);

    fireEvent.keyDown(window, { key: 'p', ctrlKey: true, shiftKey: true });
    expect(screen.getByTestId('command-palette-overlay')).toBeTruthy();
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByTestId('command-palette-overlay')).toBeNull());

    fireEvent.keyDown(window, { key: 'm', altKey: true });
    await waitFor(() => expect(spy.lines).toContain('/model'));
    const model = await screen.findByRole('dialog', { name: '/model' });
    fireEvent.keyDown(model, { key: 'Escape' });

    expect(screen.getByTestId('sidebar').className).toContain('open');
    fireEvent.keyDown(window, { key: 'b', ctrlKey: true });
    await waitFor(() => expect(screen.getByTestId('sidebar').className).not.toContain('open'));
  });

  it('uses Alt+S in model choices to open Providers without taking over Ctrl+P', async () => {
    const spy = spyCommands(() => commandResult({
      command: 'model', intent: 'model',
      data: { options: [{ provider: 'deepseek', model: 'deepseek-flash' }] },
    }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    send('/model');
    const dialog = await screen.findByRole('dialog', { name: 'Models' });
    fireEvent.keyDown(dialog, { key: 'p', ctrlKey: true });
    expect(screen.queryByRole('dialog', { name: 'Settings' })).toBeNull();
    fireEvent.keyDown(dialog, { key: 's', altKey: true });
    const settings = within(await screen.findByRole('dialog', { name: 'Settings' }));
    expect(settings.getByTestId('settings-tab-providers').getAttribute('aria-current')).toBe('page');
    expect(screen.queryByRole('dialog', { name: 'Models' })).toBeNull();
  });

  it('opens the settings panel from the header and with Alt+S', async () => {
    const spy = spyCommands(() => commandResult());
    installFetch(spy.overrides);
    render(<App />);
    await ready();

    fireEvent.click(screen.getByTestId('settings-toggle'));
    expect(screen.getByTestId('settings-overlay')).toBeTruthy();
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByTestId('settings-overlay')).toBeNull());

    fireEvent.keyDown(window, { key: 's', altKey: true });
    await waitFor(() => expect(screen.getByTestId('settings-overlay')).toBeTruthy());
    // The panel reads settings through the same command API as the TUI.
    await waitFor(() => expect(spy.lines).toContain('/model'));
    expect(spy.lines).toContain('/think');
    expect(spy.lines).toContain('/context');
  });

  it('runs /undo directly from the command palette', async () => {
    const spy = spyCommands(() =>
      commandResult({ command: 'undo', intent: 'undo', message: 'Nothing to undo.' }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    fireEvent.click(screen.getByTestId('commands-toggle'));
    const paletteInput = screen.getByTestId('palette-input');
    fireEvent.change(paletteInput, { target: { value: 'und' } });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    fireEvent.keyDown(paletteInput, { key: 'Enter' });
    await waitFor(() => expect(spy.lines).toEqual(['/undo']));
    expect(spy.confirmed).toEqual([false]);
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('keeps the composer usable when the command API fails', async () => {
    installFetch({
      '/api/commands': (init) => init?.method === 'POST'
        ? json({ error: { code: 'internal_error', message: 'boom' } }, 500)
        : json({ items: CATALOG }),
    });
    render(<App />);
    await ready();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/nope' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    const dialog = await screen.findByRole('dialog', { name: 'Command failed' });
    expect(document.activeElement).toBe(dialog);
    fireEvent.keyDown(dialog, { key: 'Escape' });
    await waitFor(() => expect(document.activeElement).toBe(input));
    expect(screen.queryByRole('dialog', { name: 'Command failed' })).toBeNull();
    fireEvent.change(input, { target: { value: 'still works' } });
    expect(input.value).toBe('still works');
  });

  it('allows read-only commands while a reply streams', async () => {
    const gate: { release?: (chunk: string) => void } = {};
    let finished = false;
    const queued = [frame({ seq: 1, type: 'start', turn_id: 31 })];
    const stream: Response = {
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: async () => {
            if (queued.length) return { done: false, value: encoder.encode(queued.shift()!) };
            if (finished) return { done: true, value: undefined };
            const chunk = await new Promise<string>((resolve) => { gate.release = resolve; });
            finished = true;
            return { done: false, value: encoder.encode(chunk) };
          },
          releaseLock: () => {},
        }),
      },
    } as unknown as Response;
    const spy = spyCommands(() => commandResult({ command: 'status', message: 'Generating: yes' }));
    installFetch({ '/api/chat': () => stream, ...spy.overrides });
    render(<App />);
    await ready();
    send('long answer');
    await waitFor(() => expect(gate.release).toBeTruthy());

    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/status' } });
    // Dismiss completions without letting Escape stop the generation.
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByTestId('command-suggestions')).toBeNull();
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(spy.lines).toContain('/status'));
    const status = await screen.findByRole('dialog', { name: '/status' });
    expect(status.textContent).toContain('Generating: yes');
    expect(screen.getByTestId('message-list').textContent).not.toContain('Generating: yes');
    fireEvent.keyDown(status, { key: 'Escape' });

    gate.release?.(
      frame({ seq: 2, type: 'content_delta', text: 'finished' })
      + frame({ seq: 3, type: 'done', text: 'finished' }),
    );
    await waitFor(() =>
      expect(screen.getByTestId('assistant-message').textContent).toContain('finished'));
  });
});

describe('Session deletion', () => {
  it('asks for confirmation and leaves the session untouched on cancel', async () => {
    const calls = installFetch({ '/api/sessions': () => json({ items: SESSIONS }) });
    render(<App />);
    await ready();

    const button = screen.getByTestId('delete-s-1');
    expect(button.getAttribute('aria-label')).toContain('Earlier chat');
    fireEvent.click(button);
    expect(screen.getByTestId('delete-overlay')).toBeTruthy();
    expect(screen.getByTestId('confirm-message').textContent).toContain('This cannot be undone');

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByTestId('delete-overlay')).toBeNull();
    expect(calls.some((call) => call.startsWith('DELETE '))).toBe(false);
    expect(screen.getByTestId('session-s-1')).toBeTruthy();
  });

  it('deletes after confirmation and refreshes the sidebar', async () => {
    let deleted = false;
    const calls = installFetch({
      '/api/sessions': () => json({ items: deleted ? [] : SESSIONS }),
      'DELETE /api/sessions/s-1': () => {
        deleted = true;
        return json({ session_id: 's-1', deleted: true });
      },
    });
    render(<App />);
    await ready();

    fireEvent.click(screen.getByTestId('delete-s-1'));
    fireEvent.click(screen.getByRole('button', { name: 'Delete permanently' }));
    await waitFor(() =>
      expect(calls).toContain('DELETE /api/sessions/s-1?confirmed=true'));
    await waitFor(() => expect(screen.queryByTestId('session-s-1')).toBeNull());
    expect(screen.queryByTestId('delete-overlay')).toBeNull();
  });

  it('opens the empty view on New chat without writing a session', async () => {
    const calls = installFetch({
      'POST /api/sessions': () => json({ session_id: null, title: '', draft: true }),
    });
    render(<App />);
    await ready();

    fireEvent.click(screen.getByRole('button', { name: 'New chat' }));
    await waitFor(() => expect(calls).toContain('POST /api/sessions'));
    expect(screen.getByTestId('message-list').textContent).toContain('Syvrio');
    // No extra row: the session file only appears with the first message.
    expect(document.querySelectorAll('.session-row').length).toBe(SESSIONS.length);
  });

  it('detaches the current session into a new chat before deleting it', async () => {
    const current = { ...SESSIONS[0], id: 's-cur', title: 'Current chat', current: true };
    const history = {
      session_id: 's-cur', title: 'Current chat', items: [], current: true,
      has_earlier: false, summary: null,
    };
    const calls = installFetch({
      '/api/sessions': () => json({ items: [current] }),
      'POST /api/sessions': () => json({ session_id: null, title: '', draft: true }),
      '/api/sessions/s-cur': () => json(history),
      'DELETE /api/sessions/s-cur': () => json({ session_id: 's-cur', deleted: true }),
    });
    render(<App />);
    await ready();

    fireEvent.click(screen.getByTestId('delete-s-cur'));
    expect(screen.getByTestId('confirm-message').textContent).toContain('new empty chat');
    fireEvent.click(screen.getByRole('button', { name: 'Delete permanently' }));
    await waitFor(() => expect(calls).toContain('DELETE /api/sessions/s-cur?confirmed=true'));
    expect(calls.indexOf('POST /api/sessions')).toBeGreaterThan(-1);
    expect(calls.indexOf('POST /api/sessions')).toBeLessThan(
      calls.indexOf('DELETE /api/sessions/s-cur?confirmed=true'));
  });

  it('reports a refused deletion without disabling the composer', async () => {
    installFetch({
      'DELETE /api/sessions/s-1': () => json(
        { error: { code: 'session_protected', message: 'Cannot delete the current session; use /new first.' } },
        409,
      ),
    });
    render(<App />);
    await ready();

    fireEvent.click(screen.getByTestId('delete-s-1'));
    fireEvent.click(screen.getByRole('button', { name: 'Delete permanently' }));
    await waitFor(() =>
      expect(screen.getByTestId('message-list').textContent).toContain('Cannot delete the current session'));
    expect(screen.queryByTestId('delete-overlay')).toBeNull();
    expect(screen.getByTestId('composer-input')).toBeTruthy();
  });
});

describe('Markdown safety', () => {
  it('does not render raw HTML or javascript links', () => {
    const { container } = render(
      <Markdown text={'<script>alert(1)</script>\n\n[bad](javascript:alert(1))'} />,
    );
    expect(container.querySelector('script')).toBeNull();
    const link = container.querySelector('a');
    expect(link?.getAttribute('href') || '').not.toContain('javascript:');
  });
});
