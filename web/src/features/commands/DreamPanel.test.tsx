import { act, fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { DreamPanelState } from './commands';
import { formatLocalTimestamp } from '../../shared/time';
import { DreamPanel } from './DreamPanel';

const idle: DreamPanelState = {
  kind: 'dream', state: 'idle',
};

it('explains Dream and submits the offered command once while awaiting its receipt', async () => {
  let accept!: () => void;
  const receipt = new Promise<void>(resolve => { accept = resolve; });
  const pick = vi.fn(() => receipt);
  render(<DreamPanel panel={idle} onPick={pick} onClose={vi.fn()} />);

  const dialog = screen.getByRole('dialog', { name: 'Dream' });
  expect(dialog.textContent).toMatch(/update your profile and suggest inferences for review/i);
  const run = screen.getByRole<HTMLButtonElement>('button', { name: 'Run Dream' });
  expect(document.activeElement).toBe(run);
  fireEvent.click(run);
  expect(screen.getByRole('status').textContent).toBe('Submitting…');
  expect(run.disabled).toBe(true);
  fireEvent.click(run);
  expect(pick.mock.calls).toEqual([['/dream run']]);
  await act(async () => { accept(); await receipt; });
  expect(run.disabled).toBe(false);
});

it('offers run and status, and allows retrying a failed status request', async () => {
  const pick = vi.fn().mockRejectedValueOnce(new Error('Connection lost. Reconnect and retry.'))
    .mockResolvedValue(undefined);
  const close = vi.fn();
  const { rerender } = render(<DreamPanel panel={{ ...idle, state: 'queued' }} onPick={pick} onClose={close} />);

  expect(screen.getByText(/queued to run/)).toBeTruthy();
  expect(screen.getAllByRole('button')).toHaveLength(3);
  const refresh = screen.getByRole<HTMLButtonElement>('button', { name: 'Refresh Dream status' });
  fireEvent.click(refresh);
  expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Run Dream' }).disabled).toBe(true);
  expect((await screen.findByRole('alert')).textContent).toMatch(/Reconnect and retry/);
  expect(refresh.disabled).toBe(false);
  await act(async () => { fireEvent.click(refresh); });
  expect(screen.queryByRole('alert')).toBeNull();
  expect(pick.mock.calls).toEqual([['/dream status'], ['/dream status']]);
  rerender(<DreamPanel panel={{ ...idle, state: 'running' }} onPick={pick} onClose={close} />);
  expect(screen.getByText('Dream is running in the background.')).toBeTruthy();
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
  expect(close).toHaveBeenCalledOnce();
});

it('explains delayed retries and terminal failures without exposing model output', () => {
  const retryAt = Date.UTC(2100, 1, 3, 4, 5) / 1000;
  const delayed = { ...idle, state: 'queued', errorKind: 'ValidationError', failures: 2, retryAt };
  const pick = vi.fn();
  const close = vi.fn();
  const { rerender } = render(<DreamPanel panel={delayed} onPick={pick} onClose={close} />);
  const localTime = formatLocalTimestamp(new Date(retryAt * 1000).toISOString());
  expect(screen.getByText(`The last model response failed validation. Next retry at ${localTime}.`)).toBeTruthy();

  rerender(<DreamPanel panel={{ ...delayed, retryAt: 1, errorKind: 'TimeoutError' }} onPick={pick} onClose={close} />);
  expect(screen.getByText('The last attempt timed out. A retry is pending.')).toBeTruthy();

  rerender(<DreamPanel panel={{ ...delayed, state: 'failed', failures: 3 }} onPick={pick} onClose={close} />);
  expect(screen.getByText('Dream stopped after 3 failed attempts. The last model response failed validation.')).toBeTruthy();
  expect(screen.queryByText(/next retry/i)).toBeNull();
  rerender(<DreamPanel panel={{ ...delayed, state: 'paused', canRun: false, runHint: 'Set a positive budget.' }}
    onPick={pick} onClose={close} />);
  expect(screen.getByText('Set a positive budget.')).toBeTruthy();
  expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Run Dream' }).disabled).toBe(true);
  rerender(<DreamPanel panel={{ ...delayed, state: 'paused', canRun: true, runHint: 'The model reached its output limit. Retry this batch.' }}
    onPick={pick} onClose={close} />);
  expect(screen.getByText(/model reached its output limit.*retry this batch/i)).toBeTruthy();
  rerender(<DreamPanel panel={{ ...delayed, errorKind: 'unexpected-private-text' }} onPick={pick} onClose={close} />);
  expect(screen.queryByText(/unexpected-private-text/)).toBeNull();
});
