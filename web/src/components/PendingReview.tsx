import { useEffect, useRef, useState } from 'react';
import type { PendingAction, PendingPanel } from '../state/commands';
import { isComposing } from '../state/shortcuts';

export function PendingReview({
  panel,
  onResolve,
  onClose,
}: {
  panel: PendingPanel;
  onResolve: (action: PendingAction, id: string, text?: string) => Promise<void>;
  onClose: () => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [action, setAction] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const dialog = useRef<HTMLDivElement>(null);
  const selected = panel.rows.find((row) => row.id === selectedId);

  useEffect(() => { dialog.current?.focus(); }, []);

  async function resolve() {
    if (!selected || !action || busy) return;
    setBusy(true);
    setError('');
    try {
      await onResolve(action, selected.id, action === 'edit' ? draft?.trim() : undefined);
      setSelectedId(null);
      setDraft(null);
      setAction(null);
      dialog.current?.focus();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Unable to update the candidate.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay pending-overlay" onClick={() => { if (!busy) onClose(); }}>
      <div
        className="command-panel pending-review"
        role="dialog"
        aria-modal="true"
        aria-label={panel.title}
        tabIndex={-1}
        ref={dialog}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (isComposing(event)) return;
          if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            if (busy) return;
            setError('');
            if (action) setAction(null);
            else if (draft !== null) setDraft(null);
            else onClose();
          }
          if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && draft !== null) {
            event.preventDefault();
            if (!busy && draft.trim()) setAction('edit');
          }
        }}
      >
        <header className="pending-header">
          <div>
            <div className="confirm-title">{panel.title}</div>
            <div className="pending-meta">
              {panel.count === 0 ? 'No pending candidates' : `${panel.count} pending`}
              {panel.count > panel.rows.length && ` · Showing first ${panel.rows.length}`}
            </div>
          </div>
          <button type="button" className="icon-button" aria-label="Close pending review"
            disabled={busy} onClick={onClose}>×</button>
        </header>
        {panel.rows.length > 0 && (
          <div className="pending-body">
            <div className="panel-options pending-list" aria-label="Candidates" role="group">
              {panel.rows.map((row) => (
                <button type="button" key={row.id} aria-pressed={row.id === selectedId}
                  disabled={busy || draft !== null || action !== null}
                  onClick={() => { setSelectedId(row.id); setError(''); }}>
                  <span className="pending-meta">{row.topic}</span>
                  <span className="pending-preview">{row.text}</span>
                </button>
              ))}
            </div>
            <div className="pending-detail">
              {selected ? (
                <>
                  <div className="pending-meta">{selected.topic}</div>
                  {selected.reason && <p className="pending-meta">{selected.reason}</p>}
                  {draft === null ? (
                    <p className="pending-text">{selected.text}</p>
                  ) : (
                    <textarea aria-label="Edit candidate" autoFocus rows={5}
                      value={draft} readOnly={busy || action !== null}
                      onChange={(event) => setDraft(event.target.value)} />
                  )}
                </>
              ) : <p className="pending-meta">Select a candidate to review.</p>}
            </div>
          </div>
        )}
        {error && <div className="provider-error" role="alert">{error}</div>}
        {selected && (
          <div className="pending-footer">
            {action && (
              <div role="status">
                {action === 'reject' ? 'Reject this candidate?' : 'Save this candidate to memory?'}
              </div>
            )}
            <div className="confirm-actions">
              {action ? (
                <>
                  <button type="button" disabled={busy} onClick={() => { setAction(null); setError(''); }}>Back</button>
                  <button type="button" className="confirm-ok" disabled={busy}
                    onClick={() => void resolve()}>{busy ? 'Working…' : 'Confirm'}</button>
                </>
              ) : draft !== null ? (
                <>
                  <button type="button" onClick={() => setDraft(null)}>Cancel edit</button>
                  <button type="button" className="confirm-ok" disabled={!draft.trim()}
                    onClick={() => setAction('edit')}>Save and approve</button>
                </>
              ) : (
                <>
                  <button type="button" onClick={() => setDraft(selected.text)}>Edit</button>
                  <button type="button" onClick={() => setAction('reject')}>Reject</button>
                  <button type="button" className="confirm-ok" onClick={() => setAction('approve')}>Approve</button>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
