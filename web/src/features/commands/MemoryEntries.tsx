import { useState } from 'react';
import { api, ApiError, newRequestId } from '../../api/client';
import type { MemoryRow } from '../../types';
import { usePanelRead } from './usePanelRead';
import { Highlight, literalPattern } from '../../shared/Highlight';

function MemoryEditor({ entry, topic, onCancel, onSaved }: {
  entry?: MemoryRow; topic: string; onCancel: (refresh: boolean) => void; onSaved: () => void;
}) {
  const [name, setName] = useState(entry?.topic ?? topic);
  const [text, setText] = useState(entry?.text ?? '');
  const [requestId] = useState(newRequestId);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [missing, setMissing] = useState(false);
  const { value: latest, error: loadError, retry } = usePanelRead(undefined, async signal => {
    try { return await api.memory(entry!.id, signal); }
    catch (err) {
      if (err instanceof ApiError && err.code === 'memory_unavailable') return null;
      throw err;
    }
  }, conflict && !!entry && !missing);
  const unavailable = missing || latest === null;
  const base = latest ?? entry;
  async function save() {
    setBusy(true);
    setError('');
    try {
      if (base) await api.editMemory(base.id, text, base.revision);
      else await api.addMemory(name, text, requestId);
      onSaved();
    } catch (err) {
      if (entry && err instanceof ApiError && err.code === 'memory_changed') {
        if (conflict) retry();
        else setConflict(true);
      } else if (err instanceof ApiError && err.code === 'memory_unavailable') setMissing(true);
      else setError(err instanceof Error ? err.message : 'Save failed.');
      setConfirm(false);
    } finally { setBusy(false); }
  }
  return <div className="memory-editor">
    {unavailable && <p role="alert">This memory is no longer available to edit. You can copy your draft before closing.</p>}
    {conflict && !unavailable && <section className="memory-conflict">
      {latest ? <>
        <p role="alert">This memory changed while you were editing. {text === latest.text
          ? `Your draft already matches saved revision ${latest.revision}.`
          : `Compare your draft with revision ${latest.revision} before saving.`}</p>
        <strong>Latest saved text</strong>
        <p>{latest.text}</p>
      </> : loadError ? <>
        <p role="alert">Could not load the latest text. {loadError}</p>
        <button type="button" onClick={retry}>Retry loading</button>
      </> : <p role="status">Memory changed. Loading the latest text…</p>}
    </section>}
    {!entry && <label>Topic<input autoFocus value={name} disabled={confirm}
      onChange={e => setName(e.target.value)} /></label>}
    <label>Memory text<textarea autoFocus={!!entry} rows={4} value={text} readOnly={confirm}
      onChange={e => setText(e.target.value)} /></label>
    {error && <p role="alert">{error}</p>}
    {confirm ? <div role="group" aria-label="Confirm save">
      <p>{entry ? 'Save this change to the existing memory?' : 'Add this memory to'} {!entry && <strong>{name}?</strong>}</p>
      <button autoFocus disabled={busy} onClick={() => setConfirm(false)}>Back</button>
      <button disabled={busy} onClick={() => void save()}>{busy ? 'Saving…' : 'Confirm save'}</button>
    </div> : <div>
      <button onClick={() => onCancel(conflict || unavailable)}>Cancel</button>
      <button disabled={unavailable || (conflict && !latest) || !name.trim() || !text.trim() || text === base?.text}
        onClick={() => setConfirm(true)}>Save</button>
    </div>}
  </div>;
}

type PageProps = {
  topic: string; query: string; after: string; page: number;
  editing: string | null; setEditing: (id: string | null) => void;
  previous: () => void; next: (cursor: string) => void; changed: () => void;
};

function MemoryPage({ topic, query, after, page, editing, setEditing, previous, next, changed }: PageProps) {
  const { value, error, retry } = usePanelRead(undefined, signal => api.memories(topic, query, after, signal));
  const pattern = literalPattern(query, 'ascii');
  return <>
    <div className="memory-actions"><button type="button" disabled={editing !== null}
      onClick={() => setEditing('')}>Add memory</button></div>
    <div className="topics-scroll topics-reader-body" role="region" aria-label="Topic entries" tabIndex={0}>
      {editing === '' && <MemoryEditor topic={topic} onCancel={() => setEditing(null)} onSaved={changed} />}
      {error ? <div role="alert">{error} <button onClick={retry}>Retry</button></div>
        : !value ? <p className="topics-notice" role="status">Loading entries…</p>
        : !value.entries.length ? <p className="topics-notice">{query ? 'No matching memories.' : 'No entries in this topic.'}</p>
        : <ul className="topics-entries">{value.entries.map(row => <li key={row.id}>
          {!topic && <small>{row.topic}</small>}
          {editing === row.id ? <MemoryEditor entry={row} topic={row.topic}
            onCancel={refresh => { setEditing(null); if (refresh) retry(); }} onSaved={changed} />
            : <><p><Highlight text={row.text} pattern={pattern} /></p>
              <button disabled={editing !== null} onClick={() => setEditing(row.id)}>Edit</button></>}
        </li>)}</ul>}
    </div>
    {(page > 1 || value?.next) && <nav className="memory-pages" aria-label="Memory pages">
      <button disabled={page === 1 || editing !== null} onClick={previous}>Previous</button><span>Page {page}</span>
      <button disabled={!value?.next || editing !== null} onClick={() => value?.next && next(value.next)}>Next</button>
    </nav>}
  </>;
}

export function MemoryEntries({ topic, onChanged }: { topic: string; onChanged: () => void }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [cursors, setCursors] = useState(['']);
  const [generation, setGeneration] = useState(0);
  const [notice, setNotice] = useState('');
  const after = cursors[cursors.length - 1];
  return <>
    <label className="review-search topics-search"><input type="search" aria-label="Search memory text"
      placeholder="Search memory text" value={query} disabled={editing !== null} onChange={e => {
        setQuery(e.target.value); setCursors(['']); setNotice('');
      }} /></label>
    {notice && <p className="memory-notice" role="status">{notice}</p>}
    <MemoryPage key={JSON.stringify([query, after, generation])} topic={topic} query={query} after={after}
      editing={editing} setEditing={setEditing} page={cursors.length} previous={() => setCursors(v => v.slice(0, -1))}
      next={cursor => setCursors(v => [...v, cursor])} changed={() => {
        setEditing(null); setCursors(['']); setGeneration(v => v + 1); setNotice('Memory saved.'); onChanged();
      }} />
  </>;
}
