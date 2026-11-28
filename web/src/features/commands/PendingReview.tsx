import { useEffect, useRef, useState } from 'react';
import type { PendingAction, ReviewPanel } from './commands';
import { CommandDialog, moveOption } from './CommandUI';
import { SearchIcon } from '../../shared/Icons';
import { api } from '../../api/client';
import type { SourceVersion } from '../../types';
import { usePanelRead } from './usePanelRead';
import { formatLocalTimestamp } from '../../shared/time';
import { MaintenanceReview } from './MaintenanceReview';

const INFERENCE_FILTERS = [
  ['hypothesis', 'Pending'], ['confirmed', 'Confirmed'], ['deprecated', 'Deprecated'],
] as const;

function EvidenceDetails({ id }: { id: string }) {
  const { value: result, error, retry } = usePanelRead(undefined, signal => api.pendingEvidence(id, signal));
  if (error) return <div className="review-load-error"><span role="alert">{error}</span>
    <button type="button" onClick={retry}>Retry</button></div>;
  if (!result) return <p className="pending-meta" role="status">Loading evidence…</p>;
  const { source, related } = result;
  return <>
    {source ? <>
      <div className="pending-meta">{source.title}{source.archived && ' · Archived'}
        {source.at && <> · <time dateTime={source.at}>{formatLocalTimestamp(source.at)}</time></>}</div>
      <blockquote>{source.content || 'This message contains no text.'}</blockquote>
    </> : <p className="pending-meta">{result.state === 'unrecorded'
      ? 'No original message was recorded.' : 'The original message is unavailable.'}</p>}
    {related.length > 0 && <><strong>Related memories</strong><ul>
      {related.map(item => <li key={item.id}>{item.text ?? `Memory unavailable (${item.id})`}</li>)}
    </ul></>}
  </>;
}

function SourceFact({ value }: { value: SourceVersion }) {
  return <div className="source-fact">
    <small>{value.topic} · {value.status} · v{value.commit}</small>
    <p>{value.text}</p>
    {value.context && <details><summary>Context</summary><pre>{JSON.stringify(value.context, null, 2)}</pre></details>}
  </div>;
}

function SourceComparison({ id }: { id: string }) {
  const { value, error, retry } = usePanelRead(undefined, signal => api.inferenceSources(id, signal));
  if (error) return <div role="alert">{error} <button onClick={retry}>Retry</button></div>;
  if (!value) return <p role="status">Loading sources…</p>;
  if (!value.sources.length) return <p>No sources recorded.</p>;
  return <ul className="inference-sources">{value.sources.map(source => {
    const unchanged = source.cited && source.current && source.cited.commit === source.current.commit;
    return <li key={source.id}>
      <small>{source.id}</small>
      {unchanged && source.current ? <><strong>Unchanged source</strong><SourceFact value={source.current} /></>
        : <div className="source-comparison">
          <section><strong>When cited</strong>{source.cited ? <SourceFact value={source.cited} />
            : <p>{source.cited_commit == null ? 'Version was not recorded.' : 'Cited version unavailable.'}</p>}</section>
          <section><strong>Current</strong>{source.current ? <SourceFact value={source.current} />
            : <p>Current memory unavailable.</p>}</section>
        </div>}
    </li>;
  })}</ul>;
}

function CandidateEvidence({ id, inference }: { id: string; inference: boolean }) {
  const [open, setOpen] = useState(false);
  return <details className="candidate-evidence" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary tabIndex={0}>{inference ? 'Compare sources' : 'View evidence'}</summary>
    {open && (inference ? <SourceComparison id={id} /> : <EvidenceDetails id={id} />)}
  </details>;
}

export function PendingReview({
  panel,
  onRetry,
  onResolve,
  onClose,
}: {
  panel: ReviewPanel;
  onRetry: () => void;
  onResolve: (action: PendingAction, id: string, text?: string) => Promise<void>;
  onClose: () => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(panel.edit?.id ?? null);
  const [draft, setDraft] = useState<string | null>(() => panel.edit
    ? panel.edit.text ?? panel.rows.find(row => row.id === panel.edit?.id)?.text ?? null : null);
  const [action, setAction] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState(0);
  const [maintenance, setMaintenance] = useState(false);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const list = useRef<HTMLDivElement>(null);
  const detail = useRef<HTMLDivElement>(null);
  const confirm = useRef<HTMLButtonElement>(null);
  const pending = panel.kind === 'pending';
  const editing = draft !== null;
  const needle = query.trim().toLowerCase();
  const rows = panel.rows.filter(row =>
    (pending || row.status === INFERENCE_FILTERS[filter][0])
    && (!needle || [row.text, row.topic, row.id, row.question].some(value => value?.toLowerCase().includes(needle))));
  const selected = rows.find((row) => row.id === selectedId);
  const reviewable = selected && (pending || selected.status === 'hypothesis' || selected.status === 'confirmed');
  const unchanged = pending && action === 'edit' && draft?.trim() === selected?.text.trim();

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

  if (pending && maintenance) return <MaintenanceReview onBack={() => setMaintenance(false)} onClose={onClose} />;
  return (
    <CommandDialog title={panel.title} className={`command-panel pending-review${pending ? ' pending-drawer' : ''}`}
      overlayClass={pending ? 'pending-drawer-overlay' : ''} onClose={() => { if (!busy && !editing && !action) onClose(); }}
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
        if (busy || draft !== null || action !== null || event.target instanceof HTMLInputElement
          || event.ctrlKey || event.metaKey || event.altKey) return;
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
            <div className="pending-meta" role="status">
              {panel.loading ? (pending ? 'Loading candidates…' : 'Loading inferences…')
                : panel.loadError && !panel.rows.length ? null : pending
                ? (panel.count === 0 ? 'No pending candidates' : `${panel.count} pending`)
                : (panel.count === 0 ? 'No inferences' : `${panel.count} inferences`)}
            </div>
          </div>
          <button type="button" className="icon-button" aria-label={`Close ${panel.title.toLowerCase()}`}
            disabled={busy} onClick={onClose}>×</button>
        </header>
        {pending && <button type="button" className="maintenance-tab" disabled={busy || editing || action !== null}
          onClick={() => setMaintenance(true)}>Maintenance suggestions</button>}
        {panel.loadError && (
          <div className="review-load-error">
            <span role="alert">{panel.loadError}</span>
            <button type="button" onClick={onRetry}>Retry</button>
          </div>
        )}
        {panel.rows.length > 0 && (
          <label className="review-search">
            <SearchIcon size={16} />
            <input type="search" value={query} aria-label={pending ? 'Search pending' : 'Search inferences'}
              placeholder={pending ? 'Search pending' : 'Search inferences'} disabled={busy || editing || action !== null}
              onChange={event => { setQuery(event.target.value); setSelectedId(null); setError(''); }} />
          </label>
        )}
        {!pending && panel.rows.length > 0 && (
          <div className="review-filters" role="group" aria-label="Inference status">
            {INFERENCE_FILTERS.map(([key, label], index) => (
              <button type="button" key={key} aria-pressed={filter === index}
                disabled={busy || editing || action !== null} onClick={() => chooseFilter(index)}>{label}</button>
            ))}
          </div>
        )}
        {panel.rows.length > 0 && (
          <div className={`pending-body${selected ? ' has-selection' : ''}`}>
            <div className="panel-options pending-list" aria-label={pending ? 'Candidates' : 'Inferences'} role="group" ref={list}>
              {!rows.length && <p className="pending-meta">{needle ? 'No matches.' : 'No inferences match this status.'}</p>}
              {rows.map((row) => (
                <button type="button" key={row.id} data-row-id={row.id} aria-pressed={row.id === selectedId}
                  disabled={busy || draft !== null || action !== null}
                  onClick={() => { setSelectedId(row.id); setError(''); }}>
                  <span className="pending-meta">{row.topic}</span>
                  <span className="pending-preview">{row.text}</span>
                </button>
              ))}
            </div>
            {(!pending || selected) && <div key={pending ? selected?.id : undefined} className="pending-detail" ref={detail}>
              {selected ? (
                <>
                  <div className="pending-meta">{selected.topic}</div>
                  {selected.question && <p className="pending-meta">{selected.question}</p>}
                  {draft === null ? (
                    <p className="pending-text">{selected.text}</p>
                  ) : (
                    <textarea aria-label={pending ? 'Edit candidate' : 'Edit inference'} ref={editor} rows={5}
                      value={draft} readOnly={busy || action !== null}
                      onChange={(event) => setDraft(event.target.value)} />
                  )}
                  <CandidateEvidence key={selected.id} id={selected.id} inference={!pending} />
                </>
              ) : <p className="pending-meta">Select an item to review.</p>}
            </div>}
          </div>
        )}
        {error && <div className="provider-error" role="alert">{error}</div>}
        {reviewable && (
          <div className="pending-footer">
            {action && (
              <div role="status">
                {unchanged ? 'No changes. Save the original text to memory?'
                  : action === 'reject' ? 'Reject this item?' : pending ? 'Save this candidate to memory?'
                  : action === 'edit' ? 'Save the correction as a confirmed inference?' : 'Confirm this inference?'}
              </div>
            )}
            <div className="confirm-actions">
              {action ? (
                <>
                  <button type="button" disabled={busy} ref={unchanged ? confirm : undefined}
                    onClick={() => { setAction(null); setError(''); }}>Back</button>
                  <button type="button" className="confirm-ok" disabled={busy} ref={unchanged ? undefined : confirm}
                    onClick={() => void resolve()}>{busy ? 'Working…' : unchanged ? 'Save unchanged' : 'Confirm'}</button>
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
