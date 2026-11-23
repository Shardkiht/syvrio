import { useLayoutEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { usePanelRead } from '../hooks/usePanelRead';
import { formatLocalTimestamp } from '../time';
import type { ChatSearchHit } from '../types';
import { CommandDialog } from './CommandUI';

function ReadFailure({ error, retry }: { error: string; retry: () => void }) {
  return <div className="review-load-error"><span role="alert">{error}</span>
    <button type="button" onClick={retry}>Retry</button></div>;
}

function Matches({ query, visible, onSelect }: { query: string; visible: boolean; onSelect: (hit: ChatSearchHit) => void }) {
  const { value, error, retry } = usePanelRead(undefined, signal => api.searchChats(query, signal));
  if (!visible) return null;
  if (error) return <ReadFailure error={error} retry={retry} />;
  if (!value) return <p role="status">Searching…</p>;
  if (!value.results.length) return <p role="status">No matches.</p>;
  return <div className="panel-options chat-matches">{value.results.map((hit, index) =>
    <button type="button" key={index} onClick={() => onSelect(hit)}>
      <strong>{hit.title}</strong>
      {hit.role !== 'title' && <span>{hit.role === 'user' ? 'You' : 'Syvrio'} · {hit.excerpt}</span>}
    </button>)}</div>;
}

function MatchedMessage({ hit }: { hit: ChatSearchHit }) {
  const { value, error, retry } = usePanelRead(undefined, signal => api.searchContext(hit, signal));
  const target = useRef<HTMLElement>(null);
  useLayoutEffect(() => { target.current?.scrollIntoView?.({ block: 'start' }); }, [value]);
  if (error) return <ReadFailure error={error} retry={retry} />;
  if (!value) return <p role="status">Loading message…</p>;
  return <div className="search-context">{value.messages.map((message, i) =>
    <article key={i} ref={message.message_id === hit.message_id ? target : undefined}
      className={message.message_id === hit.message_id ? 'matched' : undefined}>
      <small>{message.role === 'user' ? 'You' : 'Syvrio'}{message.at && ' · ' + formatLocalTimestamp(message.at)}</small>
      <p>{message.content}</p>
    </article>)}</div>;
}

export function ChatSearchPanel({ onClose, onOpen }: { onClose: () => void; onOpen: (id: string) => void }) {
  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState('');
  const [hit, setHit] = useState<ChatSearchHit | null>(null);
  return <CommandDialog title="Search chats" className="command-panel archive-panel" onClose={onClose}>
    <header className="pending-header"><strong>{hit?.title ?? 'Search chats'}</strong>
      <button className="icon-button" aria-label="Close search" onClick={onClose}>×</button></header>
    {hit ? <>
      <div className="archive-controls"><button onClick={() => setHit(null)}>Back</button>
        <button onClick={() => onOpen(hit.session_id)}>Open chat</button></div>
      <MatchedMessage hit={hit} />
    </> :
      <form className="review-search" onSubmit={e => { e.preventDefault(); setQuery(draft.trim()); }}>
        <input autoFocus type="search" maxLength={160} aria-label="Chat keyword" placeholder="Search titles and messages"
          value={draft} onChange={e => { setDraft(e.target.value); setQuery(''); }} />
        <button type="submit" disabled={!draft.trim() || query === draft.trim()}>Search</button>
      </form>}
    {query && <Matches key={query} query={query} visible={!hit} onSelect={item => {
      if (item.message_id) setHit(item); else onOpen(item.session_id);
    }} />}
  </CommandDialog>;
}
