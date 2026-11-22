import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SettingsPanel } from './SettingsPanel';
import { SHORTCUT_HELP } from '../state/shortcuts';
import type { CommandResponse } from '../types';

const HEALTH = {
  status: 'ok', version: '0.4.0.dev0', provider: 'deepseek', model: 'deepseek-flash',
  busy: false, active_turn_id: null, single_client: true, static_ready: true,
};

function json(payload: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => payload } as unknown as Response;
}

const commandResult = (patch: Partial<CommandResponse>): CommandResponse => ({
  status: 'ok', command: '', intent: '', message: 'ok', data: null,
  session_id: null, refresh_sessions: false, refresh_history: false, ...patch,
});

/** Serve the four read commands plus provider actions; record every line. */
function installFetch(initialBudget = 100000) {
  const lines: string[] = [];
  vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    if (url.startsWith('/api/settings/models?')) {
      const params = new URLSearchParams(url.split('?')[1]);
      const query = params.get('q') ?? '';
      lines.push((params.get('refresh') === 'true' ? 'refresh:' : 'search:') + query);
      const rows = [
        { provider: 'deepseek', model: 'deepseek-flash', label: 'deepseek/deepseek-flash', current: true },
        { provider: 'deepseek', model: 'deepseek-pro', label: 'deepseek/deepseek-pro', current: false },
      ];
      if (query.toLowerCase() === 'distant') rows.push({ provider: 'other', model: 'distant', label: 'Distant', current: false });
      return json({ items: rows.filter(row => row.label.toLowerCase().includes(query.toLowerCase())), unavailable_providers: [] });
    }
    if (url === '/api/settings/providers') {
      return json({ items: [
        { key: 'deepseek', label: 'DeepSeek', configured: true, current: true, endpoint: 'https://api.deepseek.com' },
      ] });
    }
    if (url === '/api/settings/providers/deepseek') {
      return json({ ok: true, message: 'Key saved.', refresh_health: true });
    }
    if (url === '/api/commands' && method === 'POST') {
      const body = JSON.parse(String(init?.body ?? '{}')) as { line: string };
      lines.push(body.line);
      if (body.line === '/think') {
        return json(commandResult({
          command: 'think', data: { current: 'high', options: ['high', 'medium', '', 'auto', 'low'] },
        }));
      }
      if (body.line === '/context') {
        return json(commandResult({
          command: 'context', data: { current: initialBudget, options: [100000, 200000, 400000, 600000] },
        }));
      }
      return json(commandResult({ command: 'model', intent: 'model', message: `applied ${body.line}` }));
    }
    return json({ error: { code: 'not_found', message: 'Not found' } }, 404);
  });
  return lines;
}

afterEach(() => vi.unstubAllGlobals());

describe('settings panel', () => {
  it('searches and refreshes the filtered catalog without submitting typed model IDs', async () => {
    const lines = installFetch();
    render(<SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />);
    await screen.findByTestId('model-deepseek-deepseek-pro');
    const search = screen.getByRole('searchbox', { name: 'Search models by name' });
    const refresh = screen.getByRole('button', { name: 'Refresh model list' });
    expect(refresh.title).toBe('Refresh model list');
    expect(search.compareDocumentPosition(refresh) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText('Switch by model ID')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Apply' })).toBeNull();
    fireEvent.change(search, { target: { value: '  PRO  ' } });
    expect(screen.queryByTestId('model-deepseek-deepseek-flash')).toBeNull();
    expect(await screen.findByTestId('model-deepseek-deepseek-pro')).toBeTruthy();
    expect(lines).toContain('search:PRO');
    fireEvent.click(refresh);
    await waitFor(() => expect(lines).toContain('refresh:PRO'));
    expect((search as HTMLInputElement).value).toBe('  PRO  ');
    fireEvent.change(search, { target: { value: 'distant' } });
    expect(await screen.findByTestId('model-other-distant')).toBeTruthy();
    fireEvent.change(search, { target: { value: 'unknown-model' } });
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(await screen.findByText('No matching models.')).toBeTruthy();
    expect(lines.filter(line => line.startsWith('/model '))).toEqual([]);
    fireEvent.change(search, { target: { value: '' } });
    fireEvent.click(await screen.findByTestId('model-deepseek-deepseek-flash'));
    await waitFor(() => expect(lines).toContain('/model deepseek/deepseek-flash'));
  });

  it('lists models and switches through /model during a reply', async () => {
    const lines = installFetch();
    const onChanged = vi.fn();
    render(
      <SettingsPanel health={{ ...HEALTH, busy: true }} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={onChanged} />,
    );

    await waitFor(() => expect(screen.getByTestId('settings-models').textContent).toContain('deepseek-pro'));
    expect(screen.getByTestId('settings-current-model').textContent).toBe('deepseek/deepseek-flash');

    expect(screen.getByRole('button', { name: 'deepseek/deepseek-flash', pressed: true })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'deepseek/deepseek-pro', pressed: false }));
    await waitFor(() => expect(lines).toContain('/model deepseek/deepseek-pro'));
    expect(await screen.findByText('applied /model deepseek/deepseek-pro')).toBeTruthy();
    expect(onChanged).toHaveBeenCalled();
  });

  it('shows reasoning options and applies one during a reply', async () => {
    const lines = installFetch();
    render(
      <SettingsPanel health={{ ...HEALTH, busy: true }} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('settings-tab-thinking'));
    await screen.findByTestId('think-auto');
    expect(screen.getByText('“default” uses the provider setting.')).toBeTruthy();
    const options = within(screen.getByRole('group', { name: 'Reasoning effort' })).getAllByRole('button');
    expect(options.map(button => button.textContent))
      .toEqual(['auto', 'default', 'low', 'medium', 'high']);
    expect(screen.getByTestId('think-high').getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByTestId('think-high'));
    await waitFor(() => expect(lines).toContain('/think high'));
  });

  it('applies a context window choice during a reply', async () => {
    const lines = installFetch();
    render(
      <SettingsPanel health={{ ...HEALTH, busy: true }} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('settings-tab-context'));
    const current = await screen.findByRole('button', { name: '100,000 tokens', pressed: true });
    expect(current.textContent).toBe('100k');
    fireEvent.click(screen.getByRole('button', { name: '200,000 tokens', pressed: false }));
    await waitFor(() => expect(lines).toContain('/context 200000'));
  });

  it('submits a custom budget as an integer token count', async () => {
    const lines = installFetch(55000);
    render(
      <SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}}
        onChanged={() => {}} initialSection="context" />,
    );
    const input = screen.getByRole('spinbutton', { name: 'Custom budget' });
    expect(await screen.findByText('Current: 55,000 tokens')).toBeTruthy();
    expect(within(screen.getByRole('group', { name: 'Context window presets' })).queryByRole('button', { pressed: true })).toBeNull();
    const apply = screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement;
    fireEvent.change(input, { target: { value: '49999' } });
    expect(apply.disabled).toBe(true);
    fireEvent.change(input, { target: { value: '50000' } });
    expect(apply.disabled).toBe(false);
    fireEvent.change(input, { target: { value: '275000' } });
    expect(apply.disabled).toBe(false);
    fireEvent.click(apply);
    await waitFor(() => expect(lines).toContain('/context 275000'));
  });

  it('reuses provider management under the providers tab', async () => {
    installFetch();
    render(
      <SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('settings-tab-providers'));
    await waitFor(() => expect(screen.getByTestId('provider-deepseek').textContent).toContain('DeepSeek'));
    expect(screen.getByTestId('provider-deepseek').textContent).toContain('configured');
  });

  it('offers automatic, light and dark appearance choices', async () => {
    installFetch();
    const onTheme = vi.fn();
    render(
      <SettingsPanel
        health={HEALTH}
        theme="auto"
        onTheme={onTheme}
        onClose={() => {}}
        onChanged={() => {}}

      />,
    );
    await screen.findByTestId('model-deepseek-deepseek-pro');
    fireEvent.click(screen.getByTestId('settings-tab-appearance'));
    const system = screen.getByRole('radio', { name: 'System', checked: true });
    fireEvent.keyDown(system, { key: 'ArrowLeft' });
    expect(onTheme).toHaveBeenCalledWith('dark');
    expect(document.activeElement).toBe(screen.getByRole('radio', { name: 'Dark' }));
    fireEvent.click(screen.getByRole('radio', { name: 'Light' }));
    expect(onTheme).toHaveBeenCalledWith('light');
  });

  it('reports the build info without exposing any secret', async () => {
    installFetch();
    render(
      <SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />,
    );
    await screen.findByTestId('model-deepseek-deepseek-pro');
    fireEvent.click(screen.getByTestId('settings-tab-about'));
    const about = screen.getByTestId('settings-about');
    expect(about.textContent).toContain('0.4.0.dev0');
    expect(about.textContent).not.toContain('Alt+S');
    expect(about.textContent).not.toMatch(/sk-/);
    fireEvent.click(screen.getByRole('button', { name: 'Shortcuts' }));
    const shortcuts = within(screen.getByRole('region', { name: 'Keyboard shortcuts' }));
    for (const { keys } of SHORTCUT_HELP) expect(shortcuts.getByText(keys)).toBeTruthy();
  });
});
