import { useState } from 'react';
import { api, newRequestId } from '../../api/client';
import type { MemoryRow } from '../../types';
import { usePanelRead } from './usePanelRead';

function MemoryEditor({ entry, topic, onCancel, onSaved }: {
  entry?: MemoryRow; topic: string; onCancel: () => void; onSaved: () => void;
}) {
  const [name, setName] = useState(entry?.topic ?? topic);
  const [text, setText] = useState(entry?.text ?? '');
  const [requestId] = useState(newRequestId);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function save() {
    setBusy(true);
    setError('');
    try {
      if (entry) await api.editMemory(entry.id, text, entry.revision);
      else await api.addMemory(name, text, requestId);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed.');
      setConfirm(false);
    } finally { setBusy(false); }
  }
  return <div className="memory-editor">
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
      <button onClick={onCancel}>Cancel</button>
      <button disabled={!name.trim() || !text.trim() || text === entry?.text}
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
            onCancel={() => setEditing(null)} onSaved={changed} />
            : <><p>{row.text}</p><button disabled={editing !== null} onClick={() => setEditing(row.id)}>Edit</button></>}
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
