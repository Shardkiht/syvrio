import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import { formatLocalTimestamp } from '../../shared/time';
import type { MaintenanceDecision, MaintenancePage, MaintenanceSuggestion } from '../../types';
import { CommandDialog } from './CommandUI';
import './maintenance.css';

function Suggestion({ item, onResolved }: { item: MaintenanceSuggestion; onResolved: () => void }) {
  const [mode, setMode] = useState<'deprecate' | 'transition'>('deprecate');
  const [selected, select] = useState('');
  const [end, setEnd] = useState('');
  const [start, setStart] = useState('');
  const [decision, setDecision] = useState<MaintenanceDecision | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const chosen = item.facts.find(fact => fact.id === selected);
  async function confirm() {
    if (!decision || busy) return;
    setBusy(true);
    setError('');
    try {
      await api.reviewMaintenance(item.id, decision);
      onResolved();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Review failed.');
      setBusy(false);
      setDecision(null);
    }
  }
  return <article className="maintenance-card">
    <h3>{item.kind === 'duplicate' ? 'Possible duplicate' : 'Possible conflict'}</h3>
    <p>{item.reason}</p>
    {item.facts.map((fact, index) => <section className="source-fact" key={fact.id}>
      <small>Fact {index + 1} · {fact.topic} · {formatLocalTimestamp(fact.recorded_at)}</small>
      <p>{fact.text}</p>
      {Object.entries(fact.context).filter(([k]) => ['event_time','valid_from','valid_to','location'].includes(k))
        .map(([k,v]) => <small key={k}>{k.replaceAll('_', ' ')} · {String(v)} </small>)}
      <details><summary>Original source</summary>
        <small>{fact.session_id ? `${fact.session_id} / ${fact.message_id}` : 'No session source recorded.'}</small>
        {fact.source_archived && <small> · Archived</small>}
        {fact.source_at && <small> · {formatLocalTimestamp(fact.source_at)}</small>}
        {fact.source != null ? <blockquote>{fact.source}</blockquote> : fact.session_id && <p>Source unavailable.</p>}
      </details>
    </section>)}
    {error && <p role="alert">{error}</p>}
    {decision ? <div className="maintenance-confirm">
      <p>{decision.action === 'ignore' ? 'Ignore this pair at these versions?' :
        decision.action === 'transition' ? `Link both facts? The previous one ends ${end}, and the selected one starts ${start}.` :
        `Keep “${chosen?.text}” and deprecate the other fact?`}</p>
      <button disabled={busy} onClick={() => setDecision(null)}>Back</button>
      <button disabled={busy} onClick={() => void confirm()}>{busy ? 'Saving…' : 'Confirm'}</button>
    </div> : <>
      {item.kind === 'conflict' && <label>Resolution <select value={mode}
        onChange={e => { setMode(e.target.value as typeof mode); select(''); }}>
        <option value="deprecate">One fact was incorrect</option>
        <option value="transition">The situation changed</option></select></label>}
      <label>{mode === 'transition' ? 'Later fact' : 'Fact to keep'} <select value={selected} onChange={e => select(e.target.value)}>
        <option value="">Choose a fact</option>{item.facts.map((f,i) => <option key={f.id} value={f.id}>Fact {i + 1}</option>)}
      </select></label>
      {mode === 'transition' && <><label>Previous fact valid through <input value={end} placeholder="YYYY, YYYY-MM or YYYY-MM-DD"
        onChange={e => setEnd(e.target.value)} /></label>
        <label>Later fact valid from <input value={start} placeholder="YYYY, YYYY-MM or YYYY-MM-DD"
          onChange={e => setStart(e.target.value)} /></label></>}
      <div><button disabled={!chosen || (mode === 'transition' && (!end.trim() || !start.trim()))}
        onClick={() => setDecision({action: mode, selected_id: selected, ...(mode === 'transition' ?
          {previous_end:end.trim(), current_start:start.trim()} : {})})}>Review change</button>
        <button onClick={() => setDecision({action:'ignore'})}>Ignore</button></div>
    </>}
  </article>;
}

export function MaintenanceReview({ onBack, onClose }: { onBack: () => void; onClose: () => void }) {
  const [pages, setPages] = useState([0]);
  const after = pages[pages.length - 1];
  const [version, refresh] = useState(0);
  const [page, setPage] = useState<MaintenancePage | null>(null);
  const [running, setRunning] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const ctl = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      try {
        const result = await api.maintenance(after, ctl.signal);
        if (ctl.signal.aborted) return;
        setPage(result);
        setError('');
        if (result.job.state === 'queued') timer = setTimeout(() => void read(), 1500);
      } catch (e) {
        if (!ctl.signal.aborted) setError(e instanceof Error ? e.message : 'Unable to load suggestions.');
      } finally {
        if (!ctl.signal.aborted) setRefreshing(false);
      }
    }
    void read();
    return () => { ctl.abort(); clearTimeout(timer); };
  }, [after, version]);
  async function run() {
    setRunning(true);
    try {
      const job = await api.runMaintenance();
      setPage(current => current && {...current, job});
      refresh(n => n + 1);
    } catch (e) { setError(e instanceof Error ? e.message : 'Unable to start maintenance.'); }
    finally { setRunning(false); }
  }
  function navigate(next: number[]) { setPage(null); setPages(next); }
  return <CommandDialog title="Maintenance suggestions" className="command-panel pending-review pending-drawer"
    overlayClass="pending-drawer-overlay" onClose={onClose}>
    <header className="pending-header"><div><button onClick={onBack}>← Pending</button>
      <h2 className="confirm-title">Maintenance suggestions</h2></div>
      <button className="icon-button" aria-label="Close maintenance" onClick={onClose}>×</button></header>
    <div className="maintenance-controls"><button disabled={running || !page || page.job.state === 'queued'} onClick={() => void run()}>
      {running ? 'Starting…' : page?.job.state === 'queued' ? 'Checking…' : 'Run check'}</button>
      <button disabled={refreshing} onClick={() => { setRefreshing(true); refresh(n => n + 1); }}>
        {refreshing ? 'Refreshing…' : 'Refresh'}</button>
      <span role="status">{page?.job.state === 'queued' ? `Audit ${page.job.cursor} / ${page.job.through}` :
        page?.job.state === 'failed' ? `Check failed · ${page.job.error}` : page?.job.state}</span></div>
    {error && <p role="alert">{error}</p>}
    {!page && !error && <p role="status">Loading suggestions…</p>}
    <div className="maintenance-list">{page?.items.map(item => <Suggestion key={item.id} item={item}
      onResolved={() => { setPage(p => p && {...p, items:p.items.filter(v => v.id !== item.id)}); refresh(n => n + 1); }} />)}
      {page && !page.items.length && <p>No maintenance suggestions on this page.</p>}</div>
    <footer>{pages.length > 1 && <button onClick={() => navigate(pages.slice(0,-1))}>Previous page</button>}
      {page?.next != null && <button onClick={() => navigate([...pages, page.next!])}>Next page</button>}</footer>
  </CommandDialog>;
}
