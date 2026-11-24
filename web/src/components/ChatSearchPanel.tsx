import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '../api/client';
import { usePanelRead } from '../hooks/usePanelRead';
import { formatLocalTimestamp } from '../time';
import type { ChatSearchHit } from '../types';
import { isComposing } from '../state/shortcuts';
import { SearchIcon } from './Icons';
import './chat-search.css';

function Highlight({ text, query }: { text: string; query: string }) {
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return <>{text.split(new RegExp('(' + escaped + ')', 'ig')).map((part, i) =>
    i % 2 ? <mark key={i}>{part}</mark> : part)}</>;
}

function ReadNotice({ message, retry }: { message: string; retry: () => void }) {
  return <div className="chat-search-notice">
    <span role="status">{message}</span>
    <button type="button" className="chat-search-action" onClick={retry}>Retry</button>
  </div>;
}

function Matches({ query, onSelect }: { query: string; onSelect: (hit: ChatSearchHit, button: HTMLButtonElement) => void }) {
  const { value, error, retry } = usePanelRead(undefined, signal => api.searchChats(query, signal));
  if (error || value?.results.length === 0) return <ReadNotice message="No results to display." retry={retry} />;
  if (!value) return <p className="chat-search-status" role="status">Searching…</p>;
  return <>
    <p className="chat-search-status" role="status">{value.results.length} {value.results.length === 1 ? 'match' : 'matches'}</p>
    <ul className="chat-search-results" aria-label="Search results">{value.results.map((hit, i) =>
      <li key={i}><button type="button" className="chat-search-result" onClick={e => onSelect(hit, e.currentTarget)}>
        <span className="chat-search-result-head">
          <strong><Highlight text={hit.title} query={query} /></strong>
          <small>{hit.role === 'title' ? 'Title' : hit.role === 'user' ? 'You' : 'Syvrio'}</small>
        </span>
        {hit.role !== 'title' && <span className="chat-search-excerpt"><Highlight text={hit.excerpt} query={query} /></span>}
      </button></li>)}</ul>
  </>;
}

function MatchedMessage({ hit, query }: { hit: ChatSearchHit; query: string }) {
  const { value, error, retry } = usePanelRead(undefined, signal => api.searchContext(hit, signal));
  const target = useRef<HTMLElement>(null);
  useLayoutEffect(() => { target.current?.scrollIntoView?.({ block: 'nearest' }); }, [value]);
  if (error) return <ReadNotice message={error} retry={retry} />;
  if (!value) return <p className="chat-search-status" role="status">Loading message…</p>;
  return <div className="search-context">{value.messages.map((message, i) => {
    const matched = message.message_id === hit.message_id;
    return <article key={i} ref={matched ? target : undefined} className={matched ? 'matched' : undefined}>
      <small>{message.role === 'user' ? 'You' : 'Syvrio'}{message.at && ' · ' + formatLocalTimestamp(message.at)}</small>
      <p>{matched ? <Highlight text={message.content} query={query} /> : message.content}</p>
    </article>;
  })}</div>;
}

export function ChatSearchPanel({ children, onOpen }: { children: ReactNode; onOpen: (id: string) => void }) {
  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState('');
  const [composing, setComposing] = useState(false);
  const [hit, setHit] = useState<ChatSearchHit | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const selected = useRef<HTMLButtonElement | null>(null);
  const keyword = draft.trim();
  const showResults = !!query && query === keyword && !composing;
  useEffect(() => {
    if (!keyword || keyword === query || composing) return;
    const timer = window.setTimeout(() => setQuery(keyword), 300);
    return () => window.clearTimeout(timer);
  }, [keyword, query, composing]);
  useLayoutEffect(() => {
    if (!hit && selected.current) {
      selected.current.focus({ preventScroll: true });
      selected.current = null;
    }
  }, [hit]);

  function clearSearch() {
    selected.current = null;
    setHit(null);
    setDraft('');
    setQuery('');
  }
  function openChat(id: string) {
    clearSearch();
    onOpen(id);
  }

  return <section className="sidebar-chat-search" aria-label="Chat history" onKeyDown={event => {
    if (event.key !== 'Escape' || isComposing(event) || composing || (!hit && !draft)) return;
    event.preventDefault();
    event.stopPropagation();
    if (hit) setHit(null);
    else { clearSearch(); input.current?.focus(); }
  }}>
    <div className="chat-search-view" hidden={!!hit}>
      <form className="chat-search-field" onSubmit={event => { event.preventDefault(); if (!composing) setQuery(keyword); }}>
        <SearchIcon size={18} />
        <input ref={input} type="search" maxLength={160} aria-label="Search chats" placeholder="Search chats"
          value={draft} onChange={event => setDraft(event.target.value)}
          onCompositionStart={() => setComposing(true)} onCompositionEnd={() => setComposing(false)} />
        {draft && <button type="button" className="icon-button" aria-label="Clear search"
          onClick={() => { clearSearch(); input.current?.focus(); }}>×</button>}
      </form>
      <div className="chat-browse" hidden={!!keyword}>{children}</div>
      {showResults && <Matches key={query} query={query} onSelect={(item, button) => {
        if (!item.message_id) { openChat(item.session_id); return; }
        selected.current = button;
        setHit(item);
      }} />}
    </div>
    {hit && <>
      <div className="chat-search-nav">
        <button type="button" className="chat-search-action" autoFocus onClick={() => setHit(null)}>Back</button>
        <button type="button" className="chat-search-action" onClick={() => openChat(hit.session_id)}>Open chat</button>
      </div>
      <h3 className="search-context-title">{hit.title}</h3>
      <MatchedMessage hit={hit} query={query} />
    </>}
  </section>;
}
