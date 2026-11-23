import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { api } from './api/client';
import { Markdown } from './components/Markdown';
import type { CommandInfo, CommandResponse, HistoryResponse, StreamEvent } from './types';
import { sessionHistory, switchResponse } from './test/sessionHistory';

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
  command('update', { support: 'client', intent: 'open_update' }),
  command('model', {
    usage: '/model [refresh]',
    subcommands: [{ name: 'refresh', description: 'Refresh model list' }],
  }),
  command('undo'),
  command('import', { support: 'client', intent: 'pick_attachments' }),
  command('help', { shortcut: 'Ctrl+Shift+P / Alt+P' }),
  command('status'),
  command('topics'),
  command('dream', {
    usage: '/dream [run|status]',
    description: 'Update your profile and generate hypotheses',
    subcommands: [{ name: 'run', description: 'Run Dream' }, { name: 'status', description: 'Show Dream status' }],
  }),
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
  type Receipt = { turn_id: number; session_id: string; text: string };
  type Job = { receipt: Receipt; start: () => Response; stream?: Response };
  const jobs = new Map<number, Job>();
  const waiting: Job[] = [];
  let active: Job | undefined;
  let paused = false;
  let session: string | null = null;
  let revision = 0;
  const advance = () => {
    active = paused ? undefined : waiting.shift();
    if (active) active.stream = active.start();
  };
  vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    calls.push(`${method} ${url}`);
    const key = url.split('?')[0];
    if ((url !== key || key === '/api/chat/queue') && overrides[`${method} ${url}`]) {
      return overrides[`${method} ${url}`](init);
    }
    if (key === '/api/chat' && new URLSearchParams(url.split('?')[1]).get('receipt') === '1' && overrides[key]) {
      const body = JSON.parse(String(init?.body));
      session = body.session_id ?? 's-1';
      const receipt = { turn_id: jobs.size + 1, session_id: session as string, text: body.message };
      const job: Job = { receipt, start: () => overrides[key](init) };
      jobs.set(receipt.turn_id, job);
      if (!active) {
        paused = false;
        job.stream = job.start();
        if (!job.stream.ok) return job.stream;
        active = job;
      } else {
        waiting.push(job);
      }
      return json(receipt, 202);
    }
    if (key === '/api/chat/queue') {
      return json({ active: active?.receipt ?? null, waiting: waiting.map(j => j.receipt), paused, revision, session_id: session });
    }
    if (key.startsWith('/api/chat/turns/')) {
      const job = jobs.get(Number(key.split('/').pop()));
      if (!job?.stream?.body) return json({}, 404);
      const raw = job.stream.body.getReader();
      return { ok: true, body: { getReader: () => ({
        read: async () => {
          const chunk = await raw.read();
          if (chunk.done && active === job) { revision++; advance(); }
          return chunk;
        },
        releaseLock: () => raw.releaseLock(),
      }) } } as Response;
    }
    if (key === '/api/commands' && method === 'POST' && JSON.parse(String(init?.body)).line === '/undo') {
      paused = waiting.length > 0;
    }
    if (overrides[`${method} ${key}`]) return overrides[`${method} ${key}`](init);
    if (overrides[key]) return overrides[key](init);
    if (key === '/api/health') return json(HEALTH);
    if (key === '/api/sessions') return json({ items: SESSIONS });
    if (key === '/api/settings/models') return json({ items: [], unavailable_providers: [] });
    if (key.endsWith('/deletion-preview')) return json({ pending: [], revision: '' });
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

function captureScrolling() {
  const calls: { node: HTMLElement; options?: ScrollIntoViewOptions }[] = [];
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: function (this: HTMLElement, options?: ScrollIntoViewOptions) {
      calls.push({ node: this, options });
    },
  });
  return calls;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  // restore the no-op from src/test/setup.ts (captureScrolling replaces it)
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    writable: true,
    value: () => {},
  });
  window.localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 1024 });
});

describe('Web client interactions', () => {
  it('edits the selected title inline, previews it in the sidebar and saves to that session only', async () => {
    const titles: string[] = [];
    const calls = installFetch({
      '/api/sessions/s-1/switch': () => json(switchResponse(sessionHistory('s-1'))),
      'PUT /api/sessions/s-1/title': init => {
        titles.push(JSON.parse(String(init?.body)).title);
        return new Response(null, { status: 204 });
      },
    });
    render(<App />);
    await ready();
    expect(screen.queryByRole('textbox', { name: 'Conversation title' })).toBeNull();
    const row = await screen.findByTestId('session-s-1');
    fireEvent.click(row);
    const input = await screen.findByRole<HTMLInputElement>('textbox', { name: 'Conversation title' });
    expect(input.value).toBe('Earlier chat');
    input.focus();
    fireEvent.change(input, { target: { value: 'Draft' } });
    expect(row.textContent).toBe('Draft');
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(document.activeElement).toBe(input);
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(row.textContent).toBe('Earlier chat');
    expect(titles).toEqual([]);
    input.focus();
    const full = '星'.repeat(79) + '🌙';
    fireEvent.change(input, { target: { value: full } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(input.readOnly).toBe(false));
    expect(titles).toEqual([full]);
    expect(input.value).toBe(full);
    expect(row.getAttribute('title')).toContain(full);
    fireEvent.change(input, { target: { value: '   ' } });
    fireEvent.blur(input);
    expect(input.value).toBe(full);
    expect(calls.filter(call => call.startsWith('PUT '))).toHaveLength(1);
  });

  it('retains a failed title draft for retry and keeps late saves scoped when switching chats', async () => {
    const other = { ...SESSIONS[0], id: 's-2', title: 'Another chat' };
    let savedTitle = SESSIONS[0].title;
    installFetch({
      'GET /api/sessions': () => json({ items: [{ ...SESSIONS[0], title: savedTitle }, other] }),
      '/api/sessions/s-1/switch': () => json(switchResponse(sessionHistory('s-1'))),
      '/api/sessions/s-2/switch': () => json(switchResponse(sessionHistory('s-2'))),
    });
    let done!: () => void;
    const save = vi.spyOn(api, 'renameSession').mockRejectedValueOnce(new Error('Offline'))
      .mockImplementationOnce((_id, title) => new Promise(resolve => {
        done = () => { savedTitle = title; resolve(); };
      }));
    try {
      render(<App />);
      await ready();
      fireEvent.click(await screen.findByTestId('session-s-1'));
      const input = await screen.findByRole<HTMLInputElement>('textbox', { name: 'Conversation title' });
      fireEvent.change(input, { target: { value: 'Retry this title' } });
      fireEvent.blur(input);
      expect(await screen.findByText('Offline', { selector: '[role="alert"]' })).toBeTruthy();
      expect(input.value).toBe('Retry this title');
      expect(screen.getByTestId('session-s-1').textContent).toBe('Earlier chat');
      input.focus();
      fireEvent.keyDown(input, { key: 'Enter' });
      expect(screen.getByText('Saving…')).toBeTruthy();
      fireEvent.click(screen.getByTestId('session-s-2'));
      await act(async () => done());
      expect(screen.getByTestId('session-s-1').textContent).toBe('Retry this title');
      expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'Conversation title' }).value).toBe('Another chat');
      expect(save.mock.calls).toEqual([['s-1', 'Retry this title'], ['s-1', 'Retry this title']]);
    } finally { save.mockRestore(); }
  });

  it('marks all replying chats in the sidebar, including a new chat from another view', async () => {
    let replying: string[] = [];
    let rows = SESSIONS;
    let selected: string | null = null;
    const idle = { ...SESSIONS[0], id: 's-idle', title: 'Quiet chat' };
    installFetch({
      'GET /api/sessions': () => json({ items: [...rows, idle] }),
      'GET /api/chat/queue': () => json({ session_id: selected, active: null, waiting: [], paused: false,
        revision: 0, replying_session_ids: replying }),
      '/api/sessions/s-idle/switch': () => { selected = idle.id; return json(switchResponse(sessionHistory(selected))); },
    });
    render(<App />);
    await ready();
    const dot = (id: string) => within(screen.getByTestId('session-' + id)).queryByRole('img', { name: 'Generating a reply' });
    expect(dot('s-1')).toBeNull();
    rows = [...SESSIONS, { ...SESSIONS[0], id: 's-2', title: 'Other chat' }];
    replying = ['s-1', 's-2'];
    await waitFor(() => { expect(dot('s-1')).toBeTruthy(); expect(dot('s-2')).toBeTruthy(); }, { timeout: 2500 });
    fireEvent.click(screen.getByTestId('session-s-idle'));
    await waitFor(() => expect(screen.getByTestId('session-s-idle').getAttribute('aria-current')).toBe('page'));
    expect(dot('s-idle')).toBeNull();
    expect(dot('s-1')).toBeTruthy();
    replying = ['s-2'];
    await waitFor(() => expect(dot('s-1')).toBeNull(), { timeout: 2500 });
    expect(dot('s-2')).toBeTruthy();
    replying = [];
    await waitFor(() => expect(dot('s-2')).toBeNull(), { timeout: 2500 });
  });

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
        case '/api/sessions/s-1/switch': return json(switchResponse(sessionHistory(current.id)));
        default: return json({}, 404);
      }
    });
    render(<App />);
    const empty = await screen.findByRole('button', { name: 'Context estimate: 0 / 4,000 tokens (0%)' });
    expect(empty.textContent).toBe('0%');
    expect(empty.getAttribute('data-wave')).toBe('still');
    expect(empty.parentElement?.nextElementSibling).toBe(screen.getByRole('button', { name: 'Send' }));
    fireEvent.click(screen.getByTestId('session-s-1'));
    const dial = await screen.findByRole('button', { name: 'Context estimate: 2,400 / 4,000 tokens (60%)' });
    expect(dial.textContent).toBe('60%');
    expect(dial.querySelector('circle')?.getAttribute('stroke-width')).toBe('2');
    expect(dial.getAttribute('data-wave')).toBe('moving');
    fireEvent.click(dial);
    expect(dial.hasAttribute('aria-expanded')).toBe(false);
    expect(dial.parentElement?.querySelector('.context-detail')?.textContent).toContain('2,400 / 4,000 tokens (60%)');
    expect(seen).toEqual([null, current.id]);
  });

  it('shows usage percentages and starts the context wave at 10% used', async () => {
    const sessions = [
      { ...SESSIONS[0], id: 's-10', title: 'Ten percent' },
      { ...SESSIONS[0], id: 's-9', title: 'Nine percent' },
    ];
    let releaseEstimate!: () => void;
    const delayedEstimate = new Promise<void>(resolve => { releaseEstimate = resolve; });
    vi.stubGlobal('fetch', async (url: string) => {
      const route = new URL(url, 'http://localhost');
      switch (route.pathname) {
        case '/api/health': return json(HEALTH);
        case '/api/sessions': return json({ items: sessions });
        case '/api/commands': return json({ items: CATALOG });
        case '/api/context-usage': {
          const id = route.searchParams.get('session_id');
          if (id === 's-9') await delayedEstimate;
          return json({
            session_id: id,
            used: id === null ? 0 : id === 's-10' ? 400 : 360,
            limit: 4000,
            compact_threshold: .8,
          });
        }
        case '/api/sessions/s-10/switch': return json(switchResponse(sessionHistory('s-10')));
        case '/api/sessions/s-9/switch': return json(switchResponse(sessionHistory('s-9')));
        default: return json({}, 404);
      }
    });
    render(<App />);
    await screen.findByRole('button', { name: /Context estimate: 0 \/ 4,000 tokens/ });

    fireEvent.click(screen.getByTestId('session-s-10'));
    const ten = await screen.findByRole('button', { name: /Context estimate: 400 \/ 4,000 tokens/ });
    expect(ten.textContent).toBe('10%');
    expect(ten.getAttribute('data-wave')).toBe('moving');

    fireEvent.click(screen.getByTestId('session-s-9'));
    const loading = await screen.findByRole('button', { name: /Context estimate: Loading estimate/ });
    expect(loading.textContent).toBe('10%');
    expect(loading.style.getPropertyValue('--context-fill')).toBe('10%');
    await act(async () => releaseEstimate());
    const nine = await screen.findByRole('button', { name: /Context estimate: 360 \/ 4,000 tokens/ });
    expect(nine.textContent).toBe('9%');
    expect(nine.getAttribute('data-wave')).toBe('still');
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
    await waitFor(() => expect(screen.getByRole('region', { name: 'Send queue' }).textContent).toContain('2 queued'));

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
            done: false, value: encoder.encode(
              frame({ seq: 1, type: 'start', turn_id: 72, session_id: 's-1' })
              + frame({ seq: 2, type: 'message_saved', session_id: 's-1', message_id: 'm-original' }),
            ),
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
        finish(frame({ seq: 3, type: 'cancelled', session_id: 's-1' }));
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
    expect(within(input.closest('.composer') as HTMLElement).queryByRole('button', { name: /undo/i })).toBeNull();
    await waitFor(() => expect(screen.getByRole('region', { name: 'Send queue' }).textContent).toContain('queued question'));
    fireEvent.click(screen.getByRole('button', { name: 'Undo last turn' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    await waitFor(() => expect(input.value).toBe('original question'));
    expect(screen.getByRole('region', { name: 'Send queue' }).textContent).toContain('paused');
    expect(sent).toEqual(['original question']);
    fireEvent.change(input, { target: { value: 'edited question' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(sent).toEqual(['original question', 'edited question', 'queued question']));
  });

  it('targets only the saved latest message, keeps cancelled edits, and replaces it in one request', async () => {
    let sends = 0;
    const history = {
      session_id: 's-1', title: 'Repeated messages', current: true, has_earlier: false, summary: null,
      items: ['m-first', 'm-latest'].map((id) => ({ id, role: 'user', content: 'old', at: '' })),
    };
    const calls = installFetch({
      '/api/sessions/s-1/switch': () => json({ session_id: history.session_id, title: history.title, history_version: 'v1', history }),
      '/api/sessions/s-1': () => json({ ...history, items: sends ? [history.items[0], { ...history.items[1], id: 'm-new', content: 'revised' },
        { ...history.items[1], id: 'a-new', role: 'assistant', content: 'answer', tool_calls: [] }] : history.items }),
      '/api/sessions/s-1/messages/m-latest/draft': () => json({ text: 'old', attachments: [] }),
      '/api/chat': (init) => {
        expect(JSON.parse(String(init?.body)).message).toBe('revised');
        sends++;
        return sse(frame({ seq: 1, type: 'message_saved', message_id: 'm-new', session_id: 's-1' })
          + frame({ seq: 2, type: 'done', text: 'answer' }));
      },
    });
    render(<App />);
    await ready();
    fireEvent.click(screen.getByTestId('session-s-1'));
    const edit = await screen.findByRole('button', { name: 'Edit and resend last message' });
    const rows = screen.getAllByTestId('user-message');
    expect(rows[0].contains(edit)).toBe(false);
    expect(rows[1].contains(edit)).toBe(true);
    fireEvent.click(edit);
    const editor = await screen.findByRole('textbox', { name: 'Edit last message' });
    fireEvent.change(editor, { target: { value: 'cancelled' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(sends).toBe(0);
    fireEvent.click(screen.getByRole('button', { name: 'Edit and resend last message' }));
    fireEvent.change(await screen.findByRole('textbox', { name: 'Edit last message' }), { target: { value: 'revised' } });
    fireEvent.click(screen.getByRole('button', { name: 'Resend' }));
    await waitFor(() => expect(sends).toBe(1));
    expect(calls).toContain('POST /api/chat?receipt=1&replace=m-latest');
    expect(calls).not.toContain('POST /api/commands');
    await screen.findByText('answer');
    expect(screen.getAllByText('old')).toHaveLength(1);
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
    const calls = installFetch({
      'POST /api/sessions/s-1/switch?include_history=true': () => json(switchResponse(history)),
    });
    render(<App />);
    await ready();
    fireEvent.click(screen.getByTestId('session-s-1'));
    await waitFor(() => expect(screen.getByTestId('message-list').textContent).toContain('earlier question'));
    expect(screen.getByTestId('message-list').textContent).toContain('earlier answer');
    expect(calls.filter(call => call.includes('/api/sessions/s-1'))).toEqual(['POST /api/sessions/s-1/switch?include_history=true']);
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
    const scrim = screen.getByTestId('sidebar-scrim');
    expect(scrim.dataset.open).toBe('true');
    fireEvent.click(scrim);
    expect(scrim.dataset.open).toBe('false');
    expect(screen.getByTestId('sidebar').hasAttribute('inert')).toBe(true);
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
    expect(screen.getByTestId('sidebar-scrim').dataset.open).toBe('false');

    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 1024 });
    fireEvent(window, new Event('resize'));
    await waitFor(() => expect(screen.getByTestId('sidebar').className).toContain('open'));
  });
});

describe('Slash commands and shortcuts', () => {
  it('opens pending from the top bar and only saves unchanged text after an explicit choice', async () => {
    const row = { id: 'p-review', topic: 'work', question: 'Keep it?', text: 'Full text\nSecond line' };
    const pending = (rows: typeof row[], extra = {}) => commandResult({
      command: 'pending', intent: 'pending', data: { kind: 'pending', rows, count: rows.length, ...extra },
    });
    const spy = spyCommands(body => body.line === '/pending' ? pending([row])
      : body.confirmed ? pending([]) : pending([row], { edit_id: row.id }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Open pending review' }));
    const drawer = await screen.findByRole('dialog', { name: 'Pending review' });
    expect(drawer.classList.contains('pending-drawer')).toBe(true);
    fireEvent.click(within(drawer).getByRole('button', { name: 'Close pending review' }));
    send('/pending edit p-review');
    const dialog = within(await screen.findByRole('dialog', { name: 'Pending review' }));
    expect((dialog.getByRole('textbox', { name: 'Edit candidate' }) as HTMLTextAreaElement).value).toBe(row.text);
    fireEvent.click(dialog.getByRole('button', { name: 'Save and approve' }));
    expect(dialog.getByText('No changes. Save the original text to memory?')).toBeTruthy();
    expect(document.activeElement).toBe(dialog.getByRole('button', { name: 'Back' }));
    fireEvent.click(dialog.getByRole('button', { name: 'Back' }));
    expect(spy.confirmed).toEqual([false, false]);
    fireEvent.click(dialog.getByRole('button', { name: 'Save and approve' }));
    fireEvent.click(dialog.getByRole('button', { name: 'Save unchanged' }));
    expect(await dialog.findByText('No pending candidates')).toBeTruthy();
    expect(spy.confirmed).toEqual([false, false, true]);
  });

  it('opens Pending before the response and keeps late results out of a reopened drawer', async () => {
    installFetch();
    const fetchOther = globalThis.fetch;
    const replies: ((response: Response) => void)[] = [];
    vi.stubGlobal('fetch', (url: string, init?: RequestInit) => {
      if (url === '/api/commands' && init?.method === 'POST')
        return new Promise<Response>(resolve => replies.push(resolve));
      return fetchOther(url, init);
    });
    render(<App />);
    await ready();
    const open = screen.getByRole('button', { name: 'Open pending review' });
    fireEvent.click(open);
    const first = screen.getByRole('dialog', { name: 'Pending review' });
    expect(within(first).getByRole('status').textContent).toBe('Loading candidates…');
    fireEvent.keyDown(first, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: 'Pending review' })).toBeNull();
    const response = (text: string) => json(commandResult({ command: 'pending', intent: 'pending',
      data: { kind: 'pending', count: 1, rows: [{ id: 'test', topic: 'test', text }] } }));
    await act(async () => replies[0](response('Stale candidate')));
    expect(screen.queryByRole('dialog', { name: 'Pending review' })).toBeNull();
    fireEvent.click(open);
    const drawer = screen.getByRole('dialog', { name: 'Pending review' });
    expect(within(drawer).getByRole('status').textContent).toBe('Loading candidates…');
    await act(async () => replies[1](response('Current candidate')));
    expect(screen.getByRole('dialog', { name: 'Pending review' })).toBe(drawer);
    expect(within(drawer).getByText('Current candidate')).toBeTruthy();
  });

  it('opens Dream status immediately and ignores its reply after another window opens', async () => {
    installFetch();
    const fetchOther = globalThis.fetch;
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', (url: string, init?: RequestInit) => {
      if (url === '/api/commands' && init?.method === 'POST') {
        return new Promise<Response>(resolve => { finish = resolve; });
      }
      return fetchOther(url, init);
    });
    render(<App />);
    await ready();
    send('/dream status');
    const dream = screen.getByRole('dialog', { name: 'Dream' });
    expect(within(dream).getByRole('status').textContent).toBe('Checking status…');
    fireEvent.keyDown(dream, { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: 'Open settings' }));
    await act(async () => finish(json(commandResult({ command: 'dream', intent: 'dream',
      data: { kind: 'dream', state: 'idle', rows: [] } }))));
    expect(screen.getByRole('dialog', { name: 'Settings' })).toBeTruthy();
    expect(screen.queryByRole('dialog', { name: 'Dream' })).toBeNull();
  });

  it('opens a session query while switching and reads the newly bound session only', async () => {
    installFetch();
    let finish!: () => void;
    const gate = new Promise<void>(resolve => { finish = resolve; });
    vi.spyOn(api, 'switchSession').mockImplementation(async id => {
      await gate;
      return switchResponse(sessionHistory(id));
    });
    const run = vi.spyOn(api, 'runCommand').mockResolvedValue(commandResult({
      command: 'summary', intent: 'summary', message: 'Summary of the selected conversation',
    }));
    render(<App />);
    await ready();
    fireEvent.click(await screen.findByTestId('session-s-1'));
    send('/summary');
    const panel = screen.getByRole('dialog', { name: 'Session summary' });
    expect(within(panel).getByRole('status').textContent).toBe('Loading…');
    expect(run).not.toHaveBeenCalled();
    await act(async () => finish());
    expect(await screen.findByText('Summary of the selected conversation')).toBeTruthy();
    expect(run).toHaveBeenCalledWith(expect.objectContaining({ line: '/summary', session_id: 's-1' }));
  });

  it('opens inference review before the response, retries in place, and ignores a stale refresh', async () => {
    installFetch();
    const other = globalThis.fetch;
    const replies: ((response: Response) => void)[] = [];
    vi.stubGlobal('fetch', (url: string, init?: RequestInit) => {
      if (url === '/api/commands' && init?.method === 'POST') {
        return new Promise<Response>(resolve => replies.push(resolve));
      }
      return other(url, init);
    });
    const reply = (labels: string[]) => json(commandResult({ command: 'inferences', intent: 'inferences',
      data: { kind: 'inferences', count: labels.length, rows: labels.map(text => ({ id: text, text, topic: 'test', status: 'hypothesis' })) } }));
    render(<App />);
    await ready();
    const open = () => {
      send('/inferences');
      return screen.getByRole('dialog', { name: 'Inference review' });
    };
    const first = open();
    expect(within(first).getByRole('status').textContent).toBe('Loading inferences…');
    expect(within(first).queryByText('No inferences')).toBeNull();
    await act(async () => replies[0](json(commandResult({ status: 'error', message: 'Read failed' }))));
    expect(within(first).getByRole('alert').textContent).toBe('Read failed');
    fireEvent.click(within(first).getByRole('button', { name: 'Retry' }));
    expect(within(first).getByRole('status').textContent).toBe('Loading inferences…');
    await act(async () => replies[1](reply(['Cached inference'])));
    expect(within(first).getByText('Cached inference')).toBeTruthy();
    fireEvent.keyDown(first, { key: 'Escape' });
    const second = open();
    expect(within(second).getByText('Cached inference')).toBeTruthy();
    fireEvent.keyDown(second, { key: 'Escape' });
    const third = open();
    await act(async () => replies[3](reply([])));
    expect(within(third).getByRole('status').textContent).toBe('No inferences');
    await act(async () => replies[2](reply(['Late response'])));
    expect(within(third).queryByText('Late response')).toBeNull();
    expect(within(third).getByRole('status').textContent).toBe('No inferences');
  });

  it('combines inference status and search over the full list', async () => {
    const rows = Array.from({ length: 26 }, (_, i) => ({
      id: `inf-${i}`, topic: 'profile', status: i < 24 ? 'confirmed' : 'hypothesis', text: `Synthetic row ${i}`,
    }));
    const spy = spyCommands(() => commandResult({ command: 'inferences', intent: 'inferences',
      data: { kind: 'inferences', rows, count: rows.length } }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    send('/inferences');
    const dialog = within(await screen.findByRole('dialog', { name: 'Inference review' }));
    const list = within(await dialog.findByRole('group', { name: 'Inferences' }));
    expect(dialog.queryByRole('button', { name: 'All' })).toBeNull();
    expect(dialog.getByRole('button', { name: 'Pending', pressed: true })).toBeTruthy();
    expect(list.getAllByRole('button')).toHaveLength(2);
    fireEvent.click(dialog.getByRole('button', { name: 'Confirmed' }));
    expect(list.getAllByRole('button')).toHaveLength(24);
    fireEvent.click(dialog.getByRole('button', { name: 'Pending' }));
    expect(list.getAllByRole('button')).toHaveLength(2);
    const search = dialog.getByRole('searchbox', { name: 'Search inferences' });
    fireEvent.change(search, { target: { value: 'INF-25' } });
    expect(list.getAllByRole('button')).toHaveLength(1);
    expect(list.getByRole('button', { name: /Synthetic row 25/ })).toBeTruthy();
    fireEvent.change(search, { target: { value: 'missing' } });
    expect(list.getByText('No matches.')).toBeTruthy();
  });

  it('selects a pending candidate once, edits it, and refreshes after confirmed writes', async () => {
    const first = { id: 'p-1', text: 'First candidate\nSecond line', topic: 'food', question: 'Check the wording' };
    const second = { id: 'p-2', text: 'Another candidate', topic: 'food', question: '' };
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
          id: 'inf-example', topic: 'profile', status: 'hypothesis', text,
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
    await dialog.findByRole('group', { name: 'Inferences' });
    fireEvent.keyDown(surface, { key: 'ArrowLeft' });
    expect(dialog.getByRole('button', { name: 'Deprecated', pressed: true })).toBeTruthy();
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

  it('checks once for /update and requires a separate confirmation to install', async () => {
    const spy = spyCommands(body => body.line === '/update'
      ? commandResult({ status: 'client', command: 'update', intent: 'open_update' })
      : commandResult());
    const calls = installFetch({
      ...spy.overrides,
      '/api/update/status': () => json({ state: 'idle', message: '' }),
      'GET /api/update': () => json({
        current: '1.0.0', latest: '1.0.1', available: true, managed: true, state: 'idle', message: '',
      }),
    });
    render(<App />);
    await ready();
    send('/update');
    const about = await screen.findByTestId('settings-about');
    const install = await within(about).findByRole('button', { name: 'Install 1.0.1' });
    expect(calls.filter(call => call === 'GET /api/update')).toHaveLength(1);
    expect(screen.queryByRole('alertdialog')).toBeNull();
    fireEvent.click(install);
    const prompt = within(screen.getByRole('alertdialog'));
    expect(prompt.getByText(/from 1\.0\.0 to 1\.0\.1/)).toBeTruthy();
    fireEvent.click(prompt.getByRole('button', { name: 'Cancel' }));
    expect(calls).not.toContain('POST /api/update');

    fireEvent.click(screen.getByTestId('settings-tab-shortcuts'));
    fireEvent.click(screen.getByTestId('settings-tab-about'));
    const check = screen.getByRole<HTMLButtonElement>('button', { name: 'Check for updates' });
    await waitFor(() => expect(check.disabled).toBe(false));
    expect(calls.filter(call => call === 'GET /api/update')).toHaveLength(1);
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

  it('opens archive and creates a chat from the Sessions dialog', async () => {
    const spy = spyCommands(() => commandResult({ status: 'client', intent: 'open_sessions' }));
    const calls = installFetch(spy.overrides);
    render(<App />);
    await ready();
    send('/sessions');
    let dialog = within(await screen.findByRole('dialog', { name: 'Sessions' }));
    fireEvent.click(dialog.getByRole('button', { name: 'Browse archive' }));
    const archive = await screen.findByRole('dialog', { name: 'Archive' });
    expect(await within(archive).findByText('Earlier chat')).toBeTruthy();
    fireEvent.keyDown(archive, { key: 'Escape' });
    send('/sessions');
    dialog = within(await screen.findByRole('dialog', { name: 'Sessions' }));
    fireEvent.click(dialog.getByRole('button', { name: 'New chat' }));
    await waitFor(() => expect(calls).toContain('POST /api/sessions'));
    expect(screen.queryByRole('dialog', { name: 'Sessions' })).toBeNull();
  });

  it('opens full thinking and context settings from their slash commands', async () => {
    const spy = spyCommands(({ line }) => commandResult({
      command: line.slice(1).split(' ')[0],
      data: line === '/think' ? { current: 'auto', options: ['auto', 'high', 'custom'] }
        : line === '/context' ? { current: 60000, options: [60000, 90000], minimum: 50000 } : {},
      message: 'Settings saved.',
    }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    send('/think');
    let dialog = await screen.findByRole('dialog', { name: 'Settings' });
    fireEvent.click(await within(dialog).findByRole('button', { name: 'custom' }));
    await waitFor(() => expect(spy.lines).toContain('/think custom'));
    fireEvent.keyDown(dialog, { key: 'Escape' });
    send('/context');
    dialog = await screen.findByRole('dialog', { name: 'Settings' });
    expect(await within(dialog).findByRole('button', { name: '90,000 tokens' })).toBeTruthy();
    fireEvent.change(within(dialog).getByRole('spinbutton', { name: 'Custom budget' }), { target: { value: '75751' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(spy.lines).toContain('/context 75751'));
  });

  it('scrolls the command palette list to follow the arrow-key selection', async () => {
    const spy = spyCommands(() => commandResult());
    installFetch(spy.overrides);
    const { rerender } = render(<App />);
    await ready();
    const calls = captureScrolling();
    fireEvent.click(screen.getByTestId('commands-toggle'));
    const palette = await screen.findByRole('dialog', { name: 'Command palette' });
    const paletteInput = screen.getByTestId('palette-input');
    fireEvent.change(paletteInput, { target: { value: 'e' } });
    fireEvent.keyDown(paletteInput, { key: 'ArrowDown' });
    const selected = palette.querySelector('.palette-list [aria-selected="true"]');
    expect(calls.length).toBeGreaterThan(0);
    expect(calls.at(-1)?.node).toBe(selected);
    expect(calls.at(-1)?.options).toEqual({ block: 'nearest', inline: 'nearest' });
    const scrollCount = calls.length;
    rerender(<App />);
    expect(calls).toHaveLength(scrollCount);
  });

  it('opens pending review from the matching field in Session status', async () => {
    const spy = spyCommands(({ line }) => line === '/status'
      ? commandResult({ data: { status: { pending_review: 0 } } })
      : commandResult({ command: 'pending', intent: 'pending', data: { kind: 'pending', rows: [], count: 0 } }));
    const calls = installFetch(spy.overrides);
    render(<App />);
    await ready();
    send('/status');
    const status = await screen.findByRole('dialog', { name: 'Session status' });
    fireEvent.click(within(status).getByRole('button', { name: 'Review pending' }));
    expect(await screen.findByRole('dialog', { name: 'Pending review' })).toBeTruthy();
    await waitFor(() => expect(spy.lines).toEqual(['/status', '/pending']));
    expect(calls.some((entry) => entry === 'POST /api/chat')).toBe(false);
  });

  it('opens the Git sync confirmation from its dialog with Ctrl+S', async () => {
    const spy = spyCommands((body) => body.line === '/git'
      ? commandResult({ command: 'git', intent: 'git_status', message: 'modified: 1' })
      : commandResult({ command: 'git', intent: 'git_status', status: 'confirm_required', message: 'Confirm sync' }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    send('/git');
    const dialog = await screen.findByRole('dialog', { name: 'Git status' });
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
    expect(input.value).toBe('/model');
    expect(screen.getByTestId('command-suggestions').textContent).not.toContain('[refresh]');
    fireEvent.change(input, { target: { value: '/model ' } });
    expect(screen.getByTestId('command-suggestions').textContent).toContain('/model refresh');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(input.value.trim()).toBe('/model refresh');
    expect(spy.lines).toEqual([]);
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(spy.lines).toEqual(['/model refresh']));
    const dialog = await screen.findByRole('dialog', { name: 'Model' });
    expect(dialog.textContent).toContain('ran /model refresh');
    expect(screen.getByTestId('message-list').textContent).not.toContain('ran /model refresh');
    expect(calls.some((entry) => entry.split('?')[0] === 'POST /api/chat')).toBe(false);
    expect(input.value).toBe('');
  });

  it('reveals Dream actions after a space and submits the parent with no numeric budget', async () => {
    const spy = spyCommands(({ confirmed }) => commandResult({
      command: 'dream', intent: 'dream', status: confirmed ? 'ok' : 'confirm_required',
      message: confirmed ? 'Dream submitted and queued.' : 'Start Dream?',
    }));
    const calls = installFetch(spy.overrides);
    render(<App />);
    await ready();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/dr' } });
    const suggestion = screen.getByRole('option', { name: /Update your profile/ });
    expect(suggestion.textContent).not.toMatch(/cancel|\[/);
    fireEvent.click(suggestion);
    expect(input.value).toBe('/dream');
    expect(screen.queryByRole('option', { name: /Run Dream|Show Dream status/i })).toBeNull();
    fireEvent.change(input, { target: { value: '/dream ' } });
    expect(screen.getAllByRole('option')).toHaveLength(2);
    expect(screen.getByRole('option', { name: /Run Dream/ }).getAttribute('aria-selected')).toBe('false');
    fireEvent.keyDown(input, { key: 'Enter' });
    const confirm = await screen.findByRole('alertdialog');
    fireEvent.click(within(confirm).getByRole('button', { name: 'Confirm' }));
    await screen.findByText('Dream submitted and queued.');
    expect(spy.lines).toEqual(['/dream', '/dream']);
    expect(spy.confirmed).toEqual([false, true]);
    expect(calls).not.toContain('POST /api/chat');
  });

  it('reads a selected topic through the same command API and returns to the filtered list', async () => {
    const topic = 'memories/garden/seed-notes.md';
    const spy = spyCommands(({ line }) => commandResult({
      command: 'topics', intent: 'topics',
      data: line === '/topics' ? { topics: [{ topic, title: 'Seed notes', entries: 1 }] }
        : { topic: 'garden/seed-notes', entries: [{ id: 'mem-seed', status: 'active', text: 'Sow after frost.' }] },
    }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    send('/topics');
    const dialog = await screen.findByRole('dialog', { name: 'Topics' });
    const search = await within(dialog).findByRole('searchbox');
    fireEvent.change(search, { target: { value: 'seed' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /Seed notes/ }));
    await within(dialog).findByText('Sow after frost.');
    fireEvent.click(within(dialog).getByRole('button', { name: /back|all topics/i }));
    expect(within(dialog).getByRole<HTMLInputElement>('searchbox').value).toBe('seed');
    expect(spy.lines).toEqual(['/topics', `/topics ${topic}`]);
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
    expect(suggestions.querySelector('[aria-selected="true"]')).toBeNull();
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(suggestions.querySelector('[aria-selected="true"]')?.textContent).toContain('/sessions');
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(suggestions.querySelector('[aria-selected="true"]')?.textContent).toContain('/new');
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByTestId('command-suggestions')).toBeNull();
    expect(spy.lines).toEqual([]);
  });

  it('scrolls the suggestions list to follow the arrow-key selection', async () => {
    const spy = spyCommands(() => commandResult());
    installFetch(spy.overrides);
    const { rerender } = render(<App />);
    await ready();
    const calls = captureScrolling();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/' } });
    const suggestions = screen.getByTestId('command-suggestions');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    const selected = suggestions.querySelector('[aria-selected="true"]');
    expect(selected?.textContent).toContain('/new');
    expect(calls.length).toBeGreaterThan(0);
    expect(calls.at(-1)?.node).toBe(selected);
    expect(calls.at(-1)?.options).toEqual({ block: 'nearest', inline: 'nearest' });
    const scrollCount = calls.length;
    rerender(<App />);
    expect(calls).toHaveLength(scrollCount);
  });

  it.each(['/', '/ne', '/dream r', '/Dr', '/dream r '])('keeps the incomplete command %s in the composer on Enter and Send', async line => {
    const spy = spyCommands(() => commandResult());
    const calls = installFetch(spy.overrides);
    render(<App />);
    await ready();
    const input = screen.getByRole<HTMLTextAreaElement>('textbox', { name: 'Message' });
    fireEvent.change(input, { target: { value: line } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByText('Complete the command before sending.')).toBeTruthy();
    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(input.value).toBe(line);
    expect(spy.lines).toEqual([]);
    expect(calls).not.toContain('POST /api/chat');
  });

  it.each(['/nope', '/dream 1', '/dream resume', '/dream cancel', '/new extra', '/sessions oops', '/import old-path'])('shows the shared invalid command response for %s', async (line) => {
    const spy = spyCommands(() => commandResult({
      status: 'unknown', command: line.split(' ')[0].slice(1), message: 'Invalid command.',
    }));
    const calls = installFetch(spy.overrides);
    render(<App />);
    await ready();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: line } });
    const suggestion = screen.queryAllByRole('option')[0];
    if (suggestion) fireEvent.mouseEnter(suggestion);
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(spy.lines).toEqual([line]));
    expect(await screen.findByRole('dialog', { name: 'Invalid command.' })).toBeTruthy();
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.getByTestId('message-list').textContent).not.toContain('Invalid command.');
    expect(calls.some((entry) => entry.split('?')[0] === 'POST /api/chat')).toBe(false);
  });

  it('opens the native multiple-file picker directly from the import command', async () => {
    const calls = installFetch();
    render(<App />);
    await ready();
    const picker = screen.getByLabelText('Select attachments') as HTMLInputElement;
    const click = vi.spyOn(picker, 'click');
    send('/import');
    expect(click).toHaveBeenCalledTimes(1);
    expect(picker.multiple).toBe(true);
    fireEvent.change(picker, { target: { files: [new File(['text'], 'notes.txt'), new File(['image'], 'photo.png')] } });
    expect(screen.getByLabelText('Remove notes.txt')).toBeTruthy();
    expect(screen.getByLabelText('Remove photo.png')).toBeTruthy();
    expect(calls.some((url) => url.startsWith('POST /api/chat'))).toBe(false);
  });

  it('rejects old typed server paths', async () => {
    const spy = spyCommands(() => commandResult({
      status: 'error', command: 'import', intent: '', message: 'Choose files on this device.',
    }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/import /tmp/private.txt' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(await screen.findByText('Choose files on this device.')).toBeTruthy();
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
    const spy = spyCommands(() => commandResult({ command: 'model', intent: 'model', message: 'Model list' }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    fireEvent.keyDown(window, { key: 'p', ctrlKey: true });
    expect(screen.queryByTestId('command-palette-overlay')).toBeNull();
    expect(spy.lines).toEqual([]);

    fireEvent.keyDown(window, { key: 'p', ctrlKey: true, shiftKey: true });
    const palette = screen.getByRole('dialog', { name: 'Command palette' });
    expect(within(palette).getByRole('option', { name: /\/help/ })).toBeTruthy();
    expect(palette.textContent).not.toContain('Ctrl+Shift+P');
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByTestId('command-palette-overlay')).toBeNull());

    fireEvent.keyDown(window, { key: 'm', altKey: true });
    const model = await screen.findByRole('dialog', { name: 'Settings' });
    expect(within(model).getByTestId('settings-tab-model').getAttribute('aria-current')).toBe('page');
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
    const dialog = await screen.findByRole('dialog', { name: 'Settings' });
    fireEvent.keyDown(dialog, { key: 'p', ctrlKey: true });
    expect(within(dialog).getByTestId('settings-tab-model').getAttribute('aria-current')).toBe('page');
    expect(within(dialog).getByRole('button', { name: 'Refresh model list' })).toBeTruthy();
    fireEvent.keyDown(dialog, { key: 's', altKey: true });
    const settings = within(await screen.findByRole('dialog', { name: 'Settings' }));
    expect(settings.getByTestId('settings-tab-providers').getAttribute('aria-current')).toBe('page');
  });

  it('opens the settings panel from the header and with Alt+S', async () => {
    const spy = spyCommands(() => commandResult());
    const calls = installFetch(spy.overrides);
    render(<App />);
    await ready();

    fireEvent.click(screen.getByTestId('settings-toggle'));
    expect(screen.getByTestId('settings-overlay')).toBeTruthy();
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByTestId('settings-overlay')).toBeNull());

    fireEvent.keyDown(window, { key: 's', altKey: true });
    await waitFor(() => expect(screen.getByTestId('settings-overlay')).toBeTruthy());
    await waitFor(() => expect(calls).toContain('GET /api/settings/models?q='));
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
    const spy = spyCommands(() => commandResult({
      command: 'status', message: 'Generating: yes', data: { status: { generating: true } },
    }));
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
    const status = await screen.findByRole('dialog', { name: 'Session status' });
    expect(within(status).getByText('Generating').nextElementSibling?.textContent).toBe('Yes');
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

describe('Session archive', () => {
  const archiveList = 'GET /api/sessions?archived=true&limit=500';
  const archivedHistory = { session_id: 's-1', title: 'Earlier chat', items: [], current: false,
    has_earlier: false, summary: null };
  const archiveReads = {
    [archiveList]: () => json({ items: SESSIONS }),
    'GET /api/sessions/s-1?archived=true': () => json(archivedHistory),
  };
  async function askToArchive(id: string) {
    fireEvent.click(await screen.findByTestId('archive-' + id));
    return within(await screen.findByTestId('archive-confirm-overlay'))
      .getByRole('alertdialog', { name: 'Archive conversation?' });
  }
  async function openArchivedChat() {
    fireEvent.click(screen.getByRole('button', { name: 'Archive' }));
    const dialog = within(await screen.findByRole('dialog', { name: 'Archive' }));
    fireEvent.click(await dialog.findByRole('button', { name: 'Earlier chat' }));
    return dialog;
  }
  async function openPurge() {
    const dialog = await openArchivedChat();
    fireEvent.click(dialog.getByRole('button', { name: 'Delete permanently…' }));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Delete permanently' }) as HTMLButtonElement).disabled).toBe(false));
  }

  it.each(['Cancel', 'Escape', 'window Escape'])('keeps a conversation on the sidebar when archive is dismissed with %s', async action => {
    const calls = installFetch();
    render(<App />);
    await ready();
    const dialog = await askToArchive('s-1');
    const cancel = within(dialog).getByRole('button', { name: 'Cancel' });
    expect(document.activeElement).toBe(cancel);
    expect(dialog.textContent).toContain('Earlier chat');
    expect(calls).not.toContain('POST /api/sessions/s-1/archive');
    if (action === 'Cancel') fireEvent.click(cancel);
    else fireEvent.keyDown(action === 'window Escape' ? window : cancel, { key: 'Escape' });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.getByTestId('session-s-1')).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('archive-s-1')));
    expect(calls.some(call => call.endsWith('/archive'))).toBe(false);
  });

  it('archives the requested sidebar row once and restores its history', async () => {
    let archived = false;
    const other = { ...SESSIONS[0], id: 's-2', title: 'Current conversation' };
    const calls = installFetch({
      ...archiveReads,
      '/api/sessions': () => json({ items: archived ? [other] : [...SESSIONS, other] }),
      '/api/sessions/s-2/switch': () => json(switchResponse(sessionHistory(other.id, 'Keep this conversation open'))),
      'POST /api/sessions/s-1/archive': () => { archived = true; return json({ archived }); },
      'POST /api/sessions/s-1/restore': () => { archived = false; return json({ archived }); },
    });
    const fetchResponse = globalThis.fetch;
    let finishSwitch!: () => void;
    const switching = new Promise<void>(resolve => { finishSwitch = resolve; });
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      const response = await fetchResponse(url, init);
      if (url.includes('/s-2/switch')) await switching;
      return response;
    });
    render(<App />);
    await ready();
    expect(calls).not.toContain(archiveList);
    fireEvent.click(screen.getByTestId('session-s-2'));
    const confirm = await askToArchive('s-1');
    finishSwitch();
    await screen.findByText('Keep this conversation open');
    expect(confirm.textContent).toContain(SESSIONS[0].title);
    expect(calls.some(call => call.endsWith('/archive'))).toBe(false);
    const archive = within(confirm).getByRole('button', { name: 'Archive' });
    fireEvent.click(archive);
    fireEvent.click(archive);
    await waitFor(() => expect(screen.queryByTestId('session-s-1')).toBeNull());
    expect(calls.filter(call => call.endsWith('/archive'))).toEqual(['POST /api/sessions/s-1/archive']);
    expect(screen.getByTestId('session-s-2').getAttribute('aria-current')).toBe('page');
    expect(screen.getByText('Keep this conversation open')).toBeTruthy();
    expect(screen.queryByRole('alertdialog')).toBeNull();
    const dialog = await openArchivedChat();
    expect(calls).toContain('GET /api/sessions/s-1?archived=true');
    fireEvent.click(dialog.getByRole('button', { name: 'Restore to chats' }));
    await waitFor(() => expect(screen.getByTestId('session-s-1')).toBeTruthy());
    expect(calls.some(call => call.startsWith('DELETE '))).toBe(false);
  });

  it('requires an explicit choice to permanently delete and keeps the archive on cancel', async () => {
    const calls = installFetch(archiveReads);
    render(<App />);
    await ready();
    await openPurge();
    const dialog = screen.getByRole('alertdialog');
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' }));
    fireEvent.keyDown(dialog, { key: 'Enter' });
    expect(screen.getByRole('alertdialog')).toBe(dialog);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    const detail = within(await screen.findByRole('dialog', { name: 'Archive' }));
    expect(await detail.findByText('Earlier chat')).toBeTruthy();
    expect(document.activeElement).toBe(detail.getByRole('button', { name: 'Delete permanently…' }));
    expect(calls.some(call => call.startsWith('DELETE '))).toBe(false);
  });

  it('keeps unsent text and attachments with the conversation where they were entered', async () => {
    const other = { ...SESSIONS[0], id: 's-2', title: 'Second chat' };
    installFetch({
      '/api/sessions': () => json({ items: [...SESSIONS, other] }),
      ...Object.fromEntries(['s-1', 's-2'].map(id => [
        '/api/sessions/' + id + '/switch', () => json(switchResponse(sessionHistory(id))),
      ])),
    });
    render(<App />);
    const select = async (id: string) => {
      const button = await screen.findByTestId('session-' + id);
      fireEvent.click(button);
      await waitFor(() => expect(button.getAttribute('aria-current')).toBe('page'));
    };
    const box = () => screen.getByRole('textbox', { name: 'Message' }) as HTMLTextAreaElement;
    await select('s-1');
    fireEvent.change(box(), { target: { value: 'Unsent in first chat' } });
    fireEvent.change(screen.getByLabelText('Select attachments'), { target: { files: [new File(['first'], 'first.txt')] } });
    await select('s-2');
    expect(box().value).toBe('');
    expect(screen.queryByLabelText('Remove first.txt')).toBeNull();
    fireEvent.change(box(), { target: { value: 'Unsent in second chat' } });
    await select('s-1');
    expect(box().value).toBe('Unsent in first chat');
    expect(screen.getByLabelText('Remove first.txt')).toBeTruthy();
    await select('s-2');
    expect(box().value).toBe('Unsent in second chat');
  });

  it('opens the empty view on New chat without writing a session', async () => {
    const calls = installFetch({
      'POST /api/sessions': () => json({ session_id: null, title: '', draft: true }),
    });
    render(<App />);
    await ready();

    fireEvent.click(screen.getByRole('button', { name: 'New chat' }));
    await waitFor(() => expect(calls).toContain('POST /api/sessions'));
    expect(within(screen.getByTestId('message-list')).getByRole('heading', { name: 'Syvrio', level: 1 })).toBeTruthy();
    // No extra row: the session file only appears with the first message.
    expect(document.querySelectorAll('.session-row').length).toBe(SESSIONS.length);
  });

  it('preserves the local draft when another tab archives the open conversation', async () => {
    let moved = false;
    installFetch({
      'GET /api/chat/queue': () => json({ active: null, waiting: [], paused: false, revision: 0,
        session_id: moved ? null : 's-1', archived_session_id: moved ? 's-1' : undefined }),
      '/api/sessions': () => json({ items: moved ? [] : [{ ...SESSIONS[0], current: true }] }),
      'GET /api/sessions/s-1': () => json({ ...archivedHistory, current: true }),
    });
    render(<App />);
    await ready();
    await waitFor(() => expect(screen.getByTestId('session-s-1').getAttribute('aria-current')).toBeTruthy());
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'Keep my unfinished message' } });
    moved = true;
    await screen.findByText('This conversation was moved to Archive.', {}, { timeout: 5500 });
    expect(input.value).toBe('Keep my unfinished message');
    expect(screen.queryByTestId('session-s-1')).toBeNull();
  });

  it('reports an active writer and leaves the conversation usable', async () => {
    installFetch({
      'POST /api/sessions/s-1/archive': () => json(
        { error: { code: 'session_protected', message: 'A background title task is running.' } }, 409),
    });
    render(<App />);
    await ready();
    const dialog = await askToArchive('s-1');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Archive' }));
    await screen.findByText('A background title task is running.');
    expect(screen.getByTestId('session-s-1')).toBeTruthy();
    expect((screen.getByTestId('composer-input') as HTMLTextAreaElement).disabled).toBe(false);
  });

  it('lists related pending text and asks again when another client changes it', async () => {
    let previews = 0;
    let deletions = 0;
    const calls = installFetch({
      ...archiveReads,
      '/api/sessions/s-1/deletion-preview': () => {
        previews++;
        return json({ pending: [{ id: 'p1', text: previews === 1 ? 'First draft' : 'Updated draft' }], revision: 'r' + previews });
      },
      'DELETE /api/sessions/s-1': () => ++deletions === 1
        ? json({ error: { code: 'pending_changed', message: 'Review the updated list.' } }, 409)
        : json({ session_id: 's-1', deleted: true }),
    });
    render(<App />);
    await ready();
    await openPurge();
    await screen.findByText('First draft');
    fireEvent.click(screen.getByRole('button', { name: 'Delete permanently' }));
    await screen.findByText('Updated draft');
    expect(deletions).toBe(1);
    expect(screen.getByText('Review the updated list.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Delete permanently' }));
    await waitFor(() => expect(calls).toContain('DELETE /api/sessions/s-1?permanent=true&confirmed=true&pending_revision=r2'));
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
