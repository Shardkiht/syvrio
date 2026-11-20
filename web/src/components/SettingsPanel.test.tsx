import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SettingsPanel } from './SettingsPanel';
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
function installFetch() {
  const lines: string[] = [];
  vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    if (url.startsWith('/api/settings/models?')) {
      const query = new URLSearchParams(url.split('?')[1]).get('q') ?? '';
      lines.push('search:' + query);
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
          command: 'context', data: { current: 100000, options: [100000, 200000, 400000, 600000] },
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
  it('searches the full catalog beside refresh without submitting typed model IDs', async () => {
    const lines = installFetch();
    render(<SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />);
    await screen.findByTestId('model-deepseek-deepseek-pro');
    const search = screen.getByRole('searchbox', { name: 'Search models by name' });
    expect(search.previousElementSibling).toBe(screen.getByRole('button', { name: 'Refresh model list' }));
    expect(screen.queryByText('Switch by model ID')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Apply' })).toBeNull();
    fireEvent.change(search, { target: { value: '  PRO  ' } });
    expect(screen.queryByTestId('model-deepseek-deepseek-flash')).toBeNull();
    expect(await screen.findByTestId('model-deepseek-deepseek-pro')).toBeTruthy();
    expect(lines).toContain('search:PRO');
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

  it('lists models and switches through the shared /model command', async () => {
    const lines = installFetch();
    const onChanged = vi.fn();
    render(
      <SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={onChanged} />,
    );

    await waitFor(() => expect(screen.getByTestId('settings-models').textContent).toContain('deepseek-pro'));
    expect(screen.getByTestId('settings-current-model').textContent).toBe('deepseek/deepseek-flash');

    fireEvent.click(screen.getByTestId('model-deepseek-deepseek-pro'));
    await waitFor(() => expect(lines).toContain('/model deepseek/deepseek-pro'));
    expect(await screen.findByText('applied /model deepseek/deepseek-pro')).toBeTruthy();
    expect(onChanged).toHaveBeenCalled();
  });

  it('shows reasoning options and applies one', async () => {
    const lines = installFetch();
    render(
      <SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('settings-tab-thinking'));
    await screen.findByTestId('think-auto');
    const options = screen.getByTestId('settings-thinking').querySelectorAll('.option-row');
    expect(Array.from(options, (button) => button.querySelector('span')?.textContent))
      .toEqual(['auto', 'default', 'low', 'medium', 'high']);
    expect(screen.getByTestId('think-high').getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByTestId('think-high'));
    await waitFor(() => expect(lines).toContain('/think high'));
  });

  it('applies a context window choice', async () => {
    const lines = installFetch();
    render(
      <SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('settings-tab-context'));
    await waitFor(() => expect(screen.getByTestId('context-200000')).toBeTruthy());
    fireEvent.click(screen.getByTestId('context-200000'));
    await waitFor(() => expect(lines).toContain('/context 200000'));
  });

  it('submits a custom budget as an integer token count', async () => {
    const lines = installFetch();
    render(
      <SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}}
        onChanged={() => {}} initialSection="context" />,
    );
    const input = screen.getByRole('spinbutton', { name: 'Custom budget' });
    const apply = screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement;
    fireEvent.change(input, { target: { value: '1000' } });
    expect(apply.disabled).toBe(true);
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
    fireEvent.click(screen.getByTestId('settings-tab-appearance'));
    expect(screen.getByTestId('theme-auto').getAttribute('aria-checked')).toBe('true');
    fireEvent.click(screen.getByTestId('theme-dark'));
    expect(onTheme).toHaveBeenCalledWith('dark');
  });

  it('reports the build info without exposing any secret', async () => {
    installFetch();
    render(
      <SettingsPanel health={HEALTH} theme="auto" onTheme={() => {}} onClose={() => {}} onChanged={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('settings-tab-about'));
    const about = screen.getByTestId('settings-about');
    expect(about.textContent).toContain('0.4.0.dev0');
    expect(about.textContent).toContain('Alt+S');
    expect(about.textContent).not.toMatch(/sk-/);
  });
});
