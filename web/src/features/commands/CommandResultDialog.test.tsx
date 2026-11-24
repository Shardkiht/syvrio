import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { resultPanelFor } from './commands';
import type { CommandResponse } from '../../types';
import { CommandResultDialog } from './CommandUI';

function response(command: string, patch: Partial<CommandResponse> = {}): CommandResponse {
  return {
    command, intent: command, status: 'ok', message: '', data: null,
    session_id: null, refresh_history: false, refresh_sessions: false, ...patch,
  };
}

describe('command results', () => {
  it('uses only known typed status fields and keeps multiline configuration values together', () => {
    const model = 'sample-model\nGenerating: yes';
    const panel = resultPanelFor(response('status', { data: { status: {
      model, generating: false, session: null, pending_review: 0, extra_field: 'irrelevant field',
    } } }));
    render(<CommandResultDialog panel={panel} title="" onRun={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole('dialog', { name: 'Session status' })).toBeTruthy();
    expect(screen.getAllByRole('heading', { level: 3 }).map(h => h.textContent)).toEqual(['Configuration', 'Conversation']);
    expect(screen.getByText('Model').nextElementSibling?.firstElementChild?.textContent).toBe(model);
    expect(screen.getByText('Generating').nextElementSibling?.textContent).toBe('No');
    expect(screen.getByText('Pending review').nextElementSibling?.firstElementChild?.textContent).toBe('0');
    expect(screen.getByText('Not created yet')).toBeTruthy();
    expect(screen.queryByText('irrelevant field')).toBeNull();
  });

  it('formats Git timestamps and preserves a legacy text-only response', () => {
    const raw = '2026-04-02T08:00:00+00:00';
    const read = (data: CommandResponse['data']) => resultPanelFor(response('git', {
      intent: 'git_status', data, message: 'modified: 2',
    }));
    const props = { title: '', onRun: vi.fn(), onClose: vi.fn() };
    const { rerender } = render(<CommandResultDialog {...props} panel={read({
      status: { branch: 'sample-branch', modified: 2, last_commit_at: raw },
    })} />);
    expect(screen.getByText('Modified files').nextElementSibling?.textContent).toBe('2');
    expect(screen.getByText('Last commit').nextElementSibling?.textContent).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    rerender(<CommandResultDialog {...props} panel={read(null)} />);
    expect(screen.getByText('modified: 2')).toBeTruthy();
  });

  it('reads the full article while keeping document links and images inert', () => {
    const panel = resultPanelFor(response('profile', {
      message: '## Reading\n\n- First point\n- Second point\n\n[Reference](https://example.invalid/ref)\n\n![Image](https://example.invalid/image.png)',
      data: { status: 'Generated profile', fact_count: 2, unique_count: 1 },
    }));
    const run = vi.fn();
    render(<CommandResultDialog panel={panel} title="" onRun={run} onClose={vi.fn()} />);
    const article = screen.getByRole('article');
    expect(within(article).getByRole('heading', { name: 'Reading' })).toBeTruthy();
    expect(within(article).getAllByRole('listitem')).toHaveLength(2);
    expect(article.querySelector('a, img, button')).toBeNull();
    expect(article.textContent).toContain('https://example.invalid/image.png');
    expect(screen.getByText('2 facts')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Run Dream' })).toBeTruthy();
    expect(run).not.toHaveBeenCalled();
  });

  it('retains the separate sync action, displays progress and permits retry after failure', async () => {
    let reject!: (error: Error) => void;
    const run = vi.fn().mockImplementationOnce(() => new Promise<void>((_, fail) => { reject = fail; }))
      .mockResolvedValueOnce(undefined);
    const close = vi.fn();
    const panel = resultPanelFor(response('git', { intent: 'git_status', data: { status: { initialized: false } } }));
    render(<CommandResultDialog panel={panel} title="" onRun={run} onClose={close} />);
    const dialog = screen.getByRole('dialog');
    fireEvent.keyDown(dialog, { ctrlKey: true, key: 's' });
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Working…' }).disabled).toBe(true);
    fireEvent.keyDown(dialog, { ctrlKey: true, key: 's' });
    expect(run.mock.calls).toEqual([['/git sync']]);
    await act(async () => reject(new Error('Connection lost.')));
    expect(screen.getByRole('alert').textContent).toBe('Connection lost.');
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Synchronize' })));
    expect(run).toHaveBeenCalledTimes(2);
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(close).toHaveBeenCalledOnce();
  });

  it('keeps command errors literal even when the failed command normally opens an article', () => {
    const panel = resultPanelFor(response('profile', { status: 'error', message: '**Error**\n# detail' }));
    render(<CommandResultDialog panel={panel} title="" onRun={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole('alert').textContent).toBe('**Error**\n# detail');
    expect(screen.queryByRole('article')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Run Dream' })).toBeNull();
  });

  it('opens the matching command from each status row and confirms a refresh even if values stay the same', async () => {
    const read = () => resultPanelFor(response('status', {
      data: { status: { provider: 'local', model: 'sample', context_window: 50_000, session: 'example', pending_review: 0 } },
    }));
    const run = vi.fn().mockResolvedValue(undefined);
    const props = { title: '', onRun: run, onClose: vi.fn() };
    const { rerender } = render(<CommandResultDialog {...props} panel={read()} />);
    for (const [field, action] of [
      ['Provider', 'Providers'], ['Model', 'Choose model'], ['Context window', 'Set window'],
      ['Session', 'Browse sessions'], ['Pending review', 'Review pending'],
    ]) {
      const row = screen.getByText(field).parentElement!;
      await act(async () => fireEvent.click(within(row).getByRole('button', { name: action })));
    }
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Refresh' })));
    expect(run.mock.calls.flat()).toEqual(['/setup', '/model', '/context', '/sessions', '/pending', '/status']);
    rerender(<CommandResultDialog {...props} panel={read()} />);
    expect(screen.getByRole('status').textContent).toBe('Refreshed');
  });

  it('offers compaction only when the summary command reports it available', async () => {
    const read = (allowed: boolean) => resultPanelFor(response('summary', {
      message: 'A short summary.', data: { can_compact: allowed },
    }));
    const run = vi.fn().mockResolvedValue(undefined);
    const props = { title: '', onRun: run, onClose: vi.fn() };
    const { rerender } = render(<CommandResultDialog {...props} panel={read(false)} />);
    expect(screen.queryByRole('button', { name: 'Compact context' })).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Refresh summary' })));
    rerender(<CommandResultDialog {...props} panel={read(true)} />);
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Compact context' })));
    expect(run.mock.calls.flat()).toEqual(['/summary', '/compact']);
  });

  it('opens the actual Dream status command from a submission receipt', async () => {
    const panel = resultPanelFor(response('dream', { message: 'Dream submitted and queued.' }));
    const run = vi.fn().mockResolvedValue(undefined);
    render(<CommandResultDialog panel={panel} title="" onRun={run} onClose={vi.fn()} />);
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Dream status' })));
    expect(run.mock.calls.flat()).toEqual(['/dream status']);
  });
});
