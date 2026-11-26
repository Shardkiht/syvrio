import { useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '../../api/client';
import { usePanelRead } from '../commands/usePanelRead';
import { isComposing } from '../../shared/shortcuts';
import { SearchIcon } from '../../shared/Icons';
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

function Matches({ query, onSelect }: { query: string; onSelect: (sessionId: string) => void }) {
  const { value, error, retry } = usePanelRead(undefined, signal => api.searchChats(query, signal));
  if (error || value?.results.length === 0) return <ReadNotice message="No results to display." retry={retry} />;
  if (!value) return <p className="chat-search-status" role="status">Searching…</p>;
  return <>
    <p className="chat-search-status" role="status">{value.results.length} {value.results.length === 1 ? 'match' : 'matches'}</p>
    <ul className="chat-search-results" aria-label="Search results">{value.results.map(hit =>
      <li key={hit.session_id + ':' + hit.message_id}><button type="button" className="chat-search-result" onClick={() => onSelect(hit.session_id)}>
        <span className="chat-search-result-head">
          <strong><Highlight text={hit.title} query={query} /></strong>
          <small>{hit.role === 'title' ? 'Title' : hit.role === 'user' ? 'You' : 'Syvrio'}</small>
        </span>
        {hit.role !== 'title' && <span className="chat-search-excerpt"><Highlight text={hit.excerpt} query={query} /></span>}
      </button></li>)}</ul>
  </>;
}

export function ChatSearchPanel({ children, onOpen }: { children: ReactNode; onOpen: (id: string) => void }) {
  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState('');
  const [composing, setComposing] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const keyword = draft.trim();
  const showResults = !!query && query === keyword && !composing;
  useEffect(() => {
    if (!keyword || keyword === query || composing) return;
    const timer = window.setTimeout(() => setQuery(keyword), 300);
    return () => window.clearTimeout(timer);
  }, [keyword, query, composing]);
  function clearSearch() {
    setDraft('');
    setQuery('');
  }
  return <section className="sidebar-chat-search" aria-label="Chat history" onKeyDown={event => {
    if (event.key !== 'Escape' || isComposing(event) || composing || !draft) return;
    event.preventDefault();
    event.stopPropagation();
    clearSearch();
    input.current?.focus();
  }}>
    <form className="chat-search-field" onSubmit={event => { event.preventDefault(); if (!composing) setQuery(keyword); }}>
      <SearchIcon size={18} />
      <input ref={input} type="search" maxLength={160} aria-label="Search chats" placeholder="Search chats"
        value={draft} onChange={event => setDraft(event.target.value)}
        onCompositionStart={() => setComposing(true)} onCompositionEnd={() => setComposing(false)} />
      {draft && <button type="button" className="icon-button" aria-label="Clear search"
        onClick={() => { clearSearch(); input.current?.focus(); }}>×</button>}
    </form>
    <div className="chat-browse" hidden={!!keyword}>{children}</div>
    {showResults && <Matches key={query} query={query} onSelect={onOpen} />}
  </section>;
}
