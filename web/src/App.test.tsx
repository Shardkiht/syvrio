import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  command('undo', { confirm: true }),
  command('import', { support: 'unsupported', note: 'Use the TUI.' }),
  command('help', { shortcut: 'Ctrl+Shift+P / Alt+P' }),
  command('status'),
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
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 1024 });
});

describe('Web client interactions', () => {
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

  it('renders tool call status and its result', async () => {
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
    expect(screen.getByTestId('tool-event').textContent).toContain('Done');
    expect(screen.getByTestId('tool-event').textContent).toContain('1 fact');
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
    await waitFor(() =>
      expect(screen.getByTestId('message-list').textContent).toContain('ran /model refresh'));
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
    expect(screen.getByTestId('message-list').textContent).toContain('Unknown command');
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
    await waitFor(() =>
      expect(screen.getByTestId('message-list').textContent).toContain('not available on the Web'));
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

    expect(screen.getByTestId('sidebar').className).toContain('open');
    fireEvent.keyDown(window, { key: 'b', ctrlKey: true });
    await waitFor(() => expect(screen.getByTestId('sidebar').className).not.toContain('open'));
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

  it('searches the palette and requires server-side confirmation for /undo', async () => {
    const spy = spyCommands((body) =>
      body.line === '/undo' && !body.confirmed
        ? commandResult({
            status: 'confirm_required', command: 'undo', intent: 'undo',
            message: 'Discards the last user turn and its reply.',
          })
        : commandResult({ command: 'undo', intent: 'undo', message: 'Nothing to undo.' }));
    installFetch(spy.overrides);
    render(<App />);
    await ready();
    fireEvent.click(screen.getByTestId('commands-toggle'));
    const paletteInput = screen.getByTestId('palette-input');
    fireEvent.change(paletteInput, { target: { value: 'und' } });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    fireEvent.keyDown(paletteInput, { key: 'Enter' });
    await waitFor(() => expect(screen.getByTestId('confirm-overlay')).toBeTruthy());
    expect(screen.getByTestId('confirm-message').textContent).toContain('Discards');
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(spy.confirmed).toContain(true));
    expect(screen.getByTestId('composer-input')).toBeTruthy();
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
    await waitFor(() =>
      expect(screen.getByTestId('message-list').textContent).toContain('Command failed'));
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
    await waitFor(() =>
      expect(screen.getByTestId('message-list').textContent).toContain('Generating: yes'));

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

  it('detaches the current session into a new chat before deleting it', async () => {
    const current = { ...SESSIONS[0], id: 's-cur', title: 'Current chat', current: true };
    const history = {
      session_id: 's-cur', title: 'Current chat', items: [], current: true,
      has_earlier: false, summary: null,
    };
    const calls = installFetch({
      '/api/sessions': () => json({ items: [current] }),
      'POST /api/sessions': () => json({ session_id: 's-new', title: 'New chat' }),
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
