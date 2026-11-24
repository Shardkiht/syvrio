import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SettingsPanel } from './SettingsPanel';
import { api } from '../api/client';
import { SHORTCUT_HELP } from '../state/shortcuts';
import type { ActionResponse, CommandResponse, ModelSearchResponse } from '../types';

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

/** 提供四个只读命令及供应商操作，并记录每一条命令。 */
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

function mockRefreshSpin(button: HTMLElement) {
  const spin = {
    currentTime: 0,
    playState: 'paused',
    effect: { getTiming: () => ({ duration: 900 }), updateTiming: vi.fn() },
    play() { this.playState = 'running'; },
  };
  Object.defineProperty(button.querySelector('svg'), 'getAnimations', { value: () => [spin] });
  return spin;
}

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

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

  it('opens one editor in its own provider card, focuses it and cancels without writing', async () => {
    installFetch();
    vi.spyOn(api, 'providers').mockResolvedValue({ items: [
      { key: 'first', label: 'First', endpoint: 'https://first.invalid/v1', current: true, configured: true },
      { key: 'second', label: 'Second', endpoint: 'https://second.invalid/v1', current: false, configured: false },
    ] });
    const write = vi.spyOn(api, 'providerAction');
    const close = vi.fn();
    render(<SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={close} onChanged={() => {}} initialSection="providers" />);
    const first = within(await screen.findByTestId('provider-first'));
    const second = within(screen.getByTestId('provider-second'));
    const add = second.getByRole('button', { name: 'Add key' });
    fireEvent.click(add);
    const input = second.getByLabelText('Second API key') as HTMLInputElement;
    expect(document.activeElement).toBe(input);
    expect(input.type).toBe('password');
    fireEvent.change(input, { target: { value: 'test-draft' } });
    fireEvent.click(first.getByRole('button', { name: 'Update key' }));
    expect(second.queryByLabelText('Second API key')).toBeNull();
    expect((first.getByLabelText('First API key') as HTMLInputElement).value).toBe('');
    const endpoint = first.getByRole('button', { name: 'Base URL' });
    fireEvent.click(endpoint);
    const url = first.getByLabelText('First Base URL') as HTMLInputElement;
    expect(url.value).toBe('https://first.invalid/v1');
    fireEvent.keyDown(url, { key: 'Escape' });
    expect(document.activeElement).toBe(endpoint);
    expect(close).not.toHaveBeenCalled();
    const remove = first.getByRole('button', { name: 'Remove' });
    fireEvent.click(remove);
    const cancel = within(first.getByRole('alertdialog', { name: 'Remove First' })).getByText('Cancel');
    expect(document.activeElement).toBe(cancel);
    fireEvent.click(cancel);
    expect(document.activeElement).toBe(remove);
    expect(write).not.toHaveBeenCalled();
  });

  it('retains a rejected key for retry, then reports the saved result on the same card', async () => {
    installFetch();
    const provider = { key: 'qianwen', label: 'Qianwen', endpoint: 'https://example.invalid/v1', current: false, configured: false };
    const list = vi.spyOn(api, 'providers').mockResolvedValue({ items: [provider] });
    const pending = deferred<ActionResponse>();
    const write = vi.spyOn(api, 'providerAction').mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce({ ok: true, message: 'Key saved.', refresh_health: false });
    const changed = vi.fn();
    render(<SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={changed} initialSection="providers" />);
    const row = within(await screen.findByTestId('provider-qianwen'));
    fireEvent.click(row.getByRole('button', { name: 'Add key' }));
    const input = row.getByLabelText('Qianwen API key') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'synthetic-value' } });
    fireEvent.click(row.getByRole('button', { name: 'Save' }));
    expect(row.getByRole('status').textContent).toMatch(/Saving/);
    fireEvent.submit(row.getByRole('form'));
    expect(write).toHaveBeenCalledTimes(1);
    pending.resolve({ ok: false, message: 'Credential store is unavailable.', refresh_health: false });
    expect((await row.findByRole('alert')).textContent).toBe('Credential store is unavailable.');
    expect(input.value).toBe('synthetic-value');
    expect(changed).not.toHaveBeenCalled();
    list.mockResolvedValue({ items: [{ ...provider, configured: true }] });
    fireEvent.click(row.getByRole('button', { name: 'Save' }));
    expect(await row.findByText('Key saved.')).toBeTruthy();
    expect(write).toHaveBeenLastCalledWith('qianwen', { action: 'set_key', api_key: 'synthetic-value' });
    expect(row.queryByRole('form')).toBeNull();
    expect(document.activeElement).toBe(row.getByRole('button', { name: 'Update key' }));
    expect(screen.queryByText('synthetic-value')).toBeNull();
    expect(changed).toHaveBeenCalledTimes(1);
    fireEvent.click(row.getByRole('button', { name: 'Use model ID' }));
    fireEvent.change(row.getByLabelText('Qianwen model ID'), { target: { value: 'qwen3.8-max' } });
    fireEvent.click(row.getByRole('button', { name: 'Apply model' }));
    expect(await row.findByText('applied /model qianwen/qwen3.8-max')).toBeTruthy();
  });

  it('sends removal only on confirmation and keeps a declined removal in its card', async () => {
    installFetch();
    const write = vi.spyOn(api, 'providerAction').mockResolvedValue({ ok: false, message: 'Switch models first.', refresh_health: false });
    render(<SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} initialSection="providers" />);
    const row = within(await screen.findByTestId('provider-deepseek'));
    fireEvent.click(row.getByRole('button', { name: 'Remove' }));
    expect(write).not.toHaveBeenCalled();
    fireEvent.click(row.getByRole('button', { name: 'Remove permanently' }));
    expect((await row.findByRole('alert')).textContent).toBe('Switch models first.');
    expect(write).toHaveBeenCalledWith('deepseek', { action: 'remove', confirmed: true });
    expect(row.getByRole('alertdialog')).toBeTruthy();
  });

  it('shares refresh progress across tabs, prevents repeat requests and confirms unchanged results', async () => {
    installFetch();
    const pending = deferred<ModelSearchResponse>();
    const read = vi.spyOn(api, 'searchModels').mockReturnValue(pending.promise);
    render(<SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} initialSection="providers" />);
    const button = screen.getByRole('button', { name: 'Refresh model list' });
    fireEvent.click(button);
    const providers = within(screen.getByTestId('providers-section'));
    expect(providers.getByText('Refreshing model list…')).toBeTruthy();
    fireEvent.click(button);
    fireEvent.click(screen.getByTestId('settings-tab-model'));
    expect(screen.getByRole('button', { name: 'Refresh model list' }).getAttribute('aria-busy')).toBe('true');
    pending.resolve({ items: [], unavailable_providers: [] });
    expect(await screen.findByText('Model list refreshed.')).toBeTruthy();
    expect(screen.getByText('No models available.')).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(1);
    expect(read).toHaveBeenCalledWith('', true, expect.any(AbortSignal));
  });

  it('shows a slow provider refresh result immediately and finishes the current revolution', async () => {
    installFetch();
    const pending = deferred<ModelSearchResponse>();
    vi.spyOn(api, 'searchModels').mockReturnValueOnce(pending.promise);
    render(<SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} initialSection="providers" />);
    const button = screen.getByRole('button', { name: 'Refresh model list' });
    const spin = mockRefreshSpin(button);
    fireEvent.click(button);
    expect(spin.playState).toBe('running');
    expect(spin.effect.updateTiming).toHaveBeenLastCalledWith({ iterations: Infinity });
    spin.currentTime = 2100;
    pending.resolve({ items: [], unavailable_providers: [] });
    expect(await screen.findByText('Model list refreshed.')).toBeTruthy();
    expect(button.getAttribute('aria-busy')).toBe('false');
    expect(spin.effect.updateTiming).toHaveBeenLastCalledWith({ iterations: 3 });

    spin.currentTime = 2700;
    spin.playState = 'finished';
    fireEvent.click(button);
    expect(spin.currentTime).toBe(0);
    expect(await screen.findByText('Model list refreshed.')).toBeTruthy();
    expect(spin.effect.updateTiming).toHaveBeenLastCalledWith({ iterations: 1 });
  });

  it('keeps existing options after an error and names unavailable providers on retry', async () => {
    installFetch();
    render(<SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />);
    await screen.findByTestId('model-deepseek-deepseek-pro');
    const read = vi.spyOn(api, 'searchModels').mockRejectedValueOnce(new Error('Offline'))
      .mockResolvedValueOnce({ items: [], unavailable_providers: ['qianwen'] });
    const button = screen.getByRole('button', { name: 'Refresh model list' });
    const spin = mockRefreshSpin(button);
    fireEvent.click(button);
    expect((await screen.findByRole('alert')).textContent).toContain('Offline');
    expect(spin.effect.updateTiming).toHaveBeenLastCalledWith({ iterations: 1 });
    expect(screen.getByTestId('model-deepseek-deepseek-pro')).toBeTruthy();
    spin.currentTime = 350;
    fireEvent.click(button);
    expect(await screen.findByText(/Catalog unavailable for qianwen/)).toBeTruthy();
    expect(spin.currentTime).toBe(350);
    expect(spin.effect.updateTiming).toHaveBeenLastCalledWith({ iterations: 2 });
    expect(screen.queryByText('Model list refreshed.')).toBeNull();
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('uses the latest filter if it changes while a refresh is in flight', async () => {
    const lines = installFetch();
    render(<SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />);
    await screen.findByTestId('model-deepseek-deepseek-pro');
    const pending = deferred<ModelSearchResponse>();
    vi.spyOn(api, 'searchModels').mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'distant' } });
    pending.resolve({ items: [], unavailable_providers: [] });
    expect(await screen.findByTestId('model-other-distant')).toBeTruthy();
    expect(lines).toContain('search:distant');
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
