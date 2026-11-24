import { useState } from 'react';
import type { DreamPanelState } from './commands';
import { formatLocalTimestamp } from '../../shared/time';
import { CommandDialog } from './CommandUI';
import { RefreshIcon } from '../../shared/Icons';

const runStatus: Record<string, string> = {
  queued: 'Dream is queued to run in the background.',
  running: 'Dream is running in the background.',
  idle: 'Ready to run.',
  paused: 'Dream is paused.',
  cancelled: 'The last run was cancelled.',
  completed: 'The last run finished.',
};

const failureReason: Record<string, string> = {
  DreamOutputTruncated: 'The model reached its output limit.',
  ValidationError: 'The last model response failed validation.',
  TimeoutError: 'The last attempt timed out.',
};

function describeRun(panel: DreamPanelState): string {
  if (panel.runHint) return panel.runHint;
  const reason = failureReason[panel.errorKind ?? ''] ?? 'The last attempt failed.';
  const retry = panel.state === 'queued' && (panel.retryAt || panel.failures || panel.errorKind);
  if (panel.state !== 'failed' && !retry) return runStatus[panel.state] ?? '';

  if (panel.state === 'failed') {
    const attempts = panel.failures && panel.failures > 1 ? ` after ${panel.failures} failed attempts` : '';
    return `Dream stopped${attempts}. ${reason}`;
  }
  if (panel.retryAt && panel.retryAt * 1000 > Date.now()) {
    const next = formatLocalTimestamp(new Date(panel.retryAt * 1000).toISOString());
    return `${reason} Next retry at ${next}.`;
  }
  return `${reason} A retry is pending.`;
}

export function DreamPanel({ panel, onPick, onClose }: {
  panel: DreamPanelState;
  onPick: (line: string) => Promise<unknown>;
  onClose: () => void;
}) {
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [error, setError] = useState('');
  const status = describeRun(panel);
  const loading = panel.state === 'loading';

  async function pick(id: 'run' | 'status') {
    if (submitting !== null) return;
    setSubmitting(id);
    setError('');
    try {
      await onPick(`/dream ${id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not submit the Dream command. Try again.');
    } finally {
      setSubmitting(null);
    }
  }

  return (
    <CommandDialog title="Dream" className="command-panel dream-panel" onClose={onClose}>
      <header className="dream-heading">
        <strong>Dream</strong>
        <button type="button" className="icon-button" aria-label="Refresh Dream status"
          title="Refresh Dream status" disabled={loading || submitting !== null} onClick={() => void pick('status')}>
          <RefreshIcon size={18} />
        </button>
      </header>
      {(loading || submitting === 'status') && <span role="status">Checking status…</span>}
      <div className="command-result-body">
        Dream uses your saved memories to update your profile and suggest inferences for review.
      </div>
      {status && <div className="confirm-message" aria-live="polite">{status}</div>}
      {error && <div role="alert" className="confirm-message">{error}</div>}
      <div className="confirm-actions">
        <button type="button" onClick={onClose}>Close</button>
        <button type="button" className="dream-run" autoFocus disabled={loading || submitting !== null || panel.canRun === false}
          onClick={() => void pick('run')}>
          {submitting === 'run' ? <span role="status">Submitting…</span> : 'Run Dream'}
        </button>
      </div>
    </CommandDialog>
  );
}
