import { useEffect, useRef, useState } from 'react';
import type { PendingAction, ReviewPanel } from '../state/commands';
import { CommandDialog, moveOption } from './CommandUI';

const INFERENCE_FILTERS = [
  ['all', 'All'], ['hypothesis', 'Pending'], ['confirmed', 'Confirmed'],
  ['deprecated', 'Deprecated'],
] as const;

export function PendingReview({
  panel,
  onResolve,
  onClose,
}: {
  panel: ReviewPanel;
  onResolve: (action: PendingAction, id: string, text?: string) => Promise<void>;
  onClose: () => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [action, setAction] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState(0);
  const [error, setError] = useState('');
  const list = useRef<HTMLDivElement>(null);
  const detail = useRef<HTMLDivElement>(null);
  const confirm = useRef<HTMLButtonElement>(null);
  const pending = panel.kind === 'pending';
  const editing = draft !== null;
  const rows = filter === 0 ? panel.rows : panel.rows.filter((row) => row.status === INFERENCE_FILTERS[filter][0]);
  const selected = rows.find((row) => row.id === selectedId);
  const reviewable = selected && (pending || selected.status === 'hypothesis' || selected.status === 'confirmed');

  const chooseFilter = (index: number) => {
    setFilter(index);
    setSelectedId(null);
    setError('');
  };

  useEffect(() => { if (action) confirm.current?.focus(); }, [action]);
  const editor = useRef<HTMLTextAreaElement>(null);
  const editButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (!action && editing) editor.current?.focus(); }, [action, editing]);
  useEffect(() => {
    if (action || draft !== null || document.activeElement !== document.body) return;
    (editButton.current ?? list.current?.querySelector<HTMLButtonElement>('button'))?.focus();
  }, [action, draft, filter, selectedId]);

  async function resolve() {
    if (!selected || !action || busy) return;
    setBusy(true);
    setError('');
    try {
      await onResolve(action, selected.id, action === 'edit' ? draft?.trim() : undefined);
      setSelectedId(null);
      setDraft(null);
      setAction(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Unable to update the candidate.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <CommandDialog title={panel.title} className="command-panel pending-review"
      overlayClass="pending-overlay" onClose={() => { if (!busy && !editing && !action) onClose(); }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          if (busy) return;
          setError('');
          if (action) {
            setAction(null);
          } else if (draft !== null) {
            setDraft(null);
          } else {
            onClose();
          }
          return;
        }
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && draft !== null) {
          event.preventDefault();
          if (!busy && !action && draft.trim()) setAction('edit');
          return;
        }
        if (busy || draft !== null || action !== null || event.ctrlKey || event.metaKey || event.altKey) return;
        if (!pending && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
          event.preventDefault();
          const step = event.key === 'ArrowLeft' ? -1 : 1;
          chooseFilter((filter + step + INFERENCE_FILTERS.length) % INFERENCE_FILTERS.length);
        } else if (event.key === 'PageUp' || event.key === 'PageDown') {
          event.preventDefault();
          detail.current?.scrollBy?.({ top: detail.current.clientHeight * (event.key === 'PageUp' ? -1 : 1) });
        } else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
          moveOption(event, Array.from(list.current?.querySelectorAll('button') ?? []));
          const id = (document.activeElement as HTMLElement | null)?.dataset.rowId;
          if (id) { setSelectedId(id); setError(''); }
        } else if (reviewable) {
          const key = event.key.toLowerCase();
          if (key === 'e') { event.preventDefault(); setDraft(selected.text); }
          else if (key === 'c' || key === 'r') {
            event.preventDefault();
            setAction(key === 'c' ? 'approve' : 'reject');
          }
        }
      }}>
        <header className="pending-header">
          <div>
            <div className="confirm-title">{panel.title}</div>
            <div className="pending-meta">
              {pending
                ? (panel.count === 0 ? 'No pending candidates' : `${panel.count} pending`)
                : (panel.count === 0 ? 'No inferences' : `${panel.count} inferences`)}
              {panel.count > panel.rows.length && ` · Showing first ${panel.rows.length}`}
            </div>
          </div>
          <button type="button" className="icon-button" aria-label={`Close ${panel.title.toLowerCase()}`}
            disabled={busy} onClick={onClose}>×</button>
        </header>
        {!pending && panel.rows.length > 0 && (
          <div className="review-filters" role="group" aria-label="Inference status">
            {INFERENCE_FILTERS.map(([key, label], index) => (
              <button type="button" key={key} aria-pressed={filter === index}
                disabled={busy || editing || action !== null} onClick={() => chooseFilter(index)}>{label}</button>
            ))}
          </div>
        )}
        {panel.rows.length > 0 && (
          <div className="pending-body">
            <div className="panel-options pending-list" aria-label={pending ? 'Candidates' : 'Inferences'} role="group" ref={list}>
              {!rows.length && <p className="pending-meta">No inferences match this status.</p>}
              {rows.map((row) => (
                <button type="button" key={row.id} data-row-id={row.id} aria-pressed={row.id === selectedId}
                  disabled={busy || draft !== null || action !== null}
                  onClick={() => { setSelectedId(row.id); setError(''); }}>
                  <span className="pending-meta">{row.topic}</span>
                  <span className="pending-preview">{row.text}</span>
                </button>
              ))}
            </div>
            <div className="pending-detail" ref={detail}>
              {selected ? (
                <>
                  <div className="pending-meta">{selected.topic}</div>
                  {selected.question && <p className="pending-meta">{selected.question}</p>}
                  {!!selected.related_ids?.length && <p className="pending-meta">{selected.related_ids.join(', ')}</p>}
                  {draft === null ? (
                    <p className="pending-text">{selected.text}</p>
                  ) : (
                    <textarea aria-label={pending ? 'Edit candidate' : 'Edit inference'} ref={editor} rows={5}
                      value={draft} readOnly={busy || action !== null}
                      onChange={(event) => setDraft(event.target.value)} />
                  )}
                </>
              ) : <p className="pending-meta">Select an item to review.</p>}
            </div>
          </div>
        )}
        {error && <div className="provider-error" role="alert">{error}</div>}
        {reviewable && (
          <div className="pending-footer">
            {action && (
              <div role="status">
                {action === 'reject' ? 'Reject this item?' : pending ? 'Save this candidate to memory?'
                  : action === 'edit' ? 'Save the correction as a confirmed inference?' : 'Confirm this inference?'}
              </div>
            )}
            <div className="confirm-actions">
              {action ? (
                <>
                  <button type="button" disabled={busy} onClick={() => { setAction(null); setError(''); }}>Back</button>
                  <button type="button" className="confirm-ok" disabled={busy} ref={confirm}
                    onClick={() => void resolve()}>{busy ? 'Working…' : 'Confirm'}</button>
                </>
              ) : draft !== null ? (
                <>
                  <button type="button" onClick={() => setDraft(null)}>Cancel edit</button>
                  <button type="button" className="confirm-ok" disabled={!draft.trim()}
                    onClick={() => setAction('edit')}>{pending ? 'Save and approve' : 'Save correction'}</button>
                </>
              ) : (
                <>
                  <button type="button" ref={editButton} onClick={() => setDraft(selected.text)}>Edit</button>
                  <button type="button" onClick={() => setAction('reject')}>Reject</button>
                  <button type="button" className="confirm-ok" onClick={() => setAction('approve')}>{pending ? 'Approve' : 'Confirm inference'}</button>
                </>
              )}
            </div>
          </div>
        )}
    </CommandDialog>
  );
}
