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
      if (body.line === '/model') {
        return json(commandResult({
          command: 'model', intent: 'model', message: 'Current model: deepseek/deepseek-flash',
          data: { options: [
            { provider: 'deepseek', model: 'deepseek-flash', label: 'deepseek/deepseek-flash', current: true },
            { provider: 'deepseek', model: 'deepseek-pro', label: 'deepseek/deepseek-pro', current: false },
          ] },
        }));
      }
      if (body.line === '/think') {
        return json(commandResult({
          command: 'think', data: { current: 'auto', options: ['', 'auto', 'high'] },
        }));
      }
      if (body.line === '/context') {
        return json(commandResult({
          command: 'context', data: { current: 128000, options: [64000, 128000] },
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
  it('lists models and switches through the shared /model command', async () => {
    const lines = installFetch();
    const onChanged = vi.fn();
    const onNotice = vi.fn();
    render(
      <SettingsPanel health={HEALTH} onClose={() => {}} onChanged={onChanged} onNotice={onNotice} />,
    );

    await waitFor(() => expect(screen.getByTestId('settings-models').textContent).toContain('deepseek-pro'));
    expect(screen.getByTestId('settings-current-model').textContent).toBe('deepseek/deepseek-flash');

    fireEvent.click(screen.getByTestId('model-deepseek-deepseek-pro'));
    await waitFor(() => expect(lines).toContain('/model deepseek/deepseek-pro'));
    expect(onNotice).toHaveBeenCalledWith('applied /model deepseek/deepseek-pro');
    expect(onChanged).toHaveBeenCalled();
  });

  it('shows reasoning options and applies one', async () => {
    const lines = installFetch();
    render(
      <SettingsPanel health={HEALTH} onClose={() => {}} onChanged={() => {}} onNotice={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('settings-tab-thinking'));
    await waitFor(() => expect(screen.getByTestId('settings-thinking').textContent).toContain('default'));
    fireEvent.click(screen.getByTestId('think-high'));
    await waitFor(() => expect(lines).toContain('/think high'));
  });

  it('applies a context window choice', async () => {
    const lines = installFetch();
    render(
      <SettingsPanel health={HEALTH} onClose={() => {}} onChanged={() => {}} onNotice={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('settings-tab-context'));
    await waitFor(() => expect(screen.getByTestId('context-64000')).toBeTruthy());
    fireEvent.click(screen.getByTestId('context-64000'));
    await waitFor(() => expect(lines).toContain('/context 64000'));
  });

  it('reuses provider management under the providers tab', async () => {
    installFetch();
    render(
      <SettingsPanel health={HEALTH} onClose={() => {}} onChanged={() => {}} onNotice={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('settings-tab-providers'));
    await waitFor(() => expect(screen.getByTestId('provider-deepseek').textContent).toContain('DeepSeek'));
    expect(screen.getByTestId('provider-deepseek').textContent).toContain('configured');
  });

  it('reports the build info without exposing any secret', async () => {
    installFetch();
    render(
      <SettingsPanel health={HEALTH} onClose={() => {}} onChanged={() => {}} onNotice={() => {}} />,
    );
    fireEvent.click(screen.getByTestId('settings-tab-about'));
    const about = screen.getByTestId('settings-about');
    expect(about.textContent).toContain('0.4.0.dev0');
    expect(about.textContent).toContain('Alt+S');
    expect(about.textContent).not.toMatch(/sk-/);
  });
});
