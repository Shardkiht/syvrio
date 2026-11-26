import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { api } from '../../api/client';
import { usePanelRead } from '../commands/usePanelRead';
import { isComposing } from '../../shared/shortcuts';
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

export function useChatSearch() {
  const [draft, setDraft] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [composing, setComposing] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const matches = useRef<HTMLDivElement>(null);
  const expanded = draft !== null;
  const keyword = draft?.trim() ?? '';

  useLayoutEffect(() => {
    if (expanded) input.current?.focus();
  }, [expanded]);
  useEffect(() => {
    if (!keyword || keyword === query || composing) return;
    const timer = window.setTimeout(() => setQuery(keyword), 300);
    return () => window.clearTimeout(timer);
  }, [keyword, query, composing]);

  const close = useCallback(() => {
    setDraft(null);
    setQuery('');
    setComposing(false);
  }, []);

  useEffect(() => {
    if (!expanded) return;
    function dismissOutside(event: MouseEvent) {
      const target = event.target;
      if (!(target instanceof Node) || trigger.current?.contains(target)
        || input.current?.form?.contains(target) || matches.current?.contains(target)) return;
      close();
    }
    document.addEventListener('click', dismissOutside, true);
    return () => document.removeEventListener('click', dismissOutside, true);
  }, [expanded, close]);

  function toggle() {
    if (expanded) close();
    else setDraft('');
  }
  function onKeyDown(event: KeyboardEvent) {
    if (!expanded || event.key !== 'Escape' || isComposing(event) || composing) return;
    event.preventDefault();
    event.stopPropagation();
    close();
    trigger.current?.focus();
  }
  function clear() {
    setDraft('');
    setQuery('');
    input.current?.focus();
  }

  return {
    draft, setDraft, expanded, keyword, setComposing, input, trigger, matches, toggle, onKeyDown, clear,
    submit: () => { if (!composing) setQuery(keyword); },
    resultQuery: !composing && keyword === query ? query : '',
  };
}

type SearchState = ReturnType<typeof useChatSearch>;

export function ChatSearchField({ search }: { search: SearchState }) {
  return <form id="sidebar-chat-search-field" className="chat-search-field" aria-hidden={!search.expanded}
    onSubmit={event => { event.preventDefault(); search.submit(); }}>
    <input ref={search.input} disabled={!search.expanded} type="search" maxLength={160} aria-label="Search chats"
      placeholder="Search chats" value={search.draft ?? ''} onChange={event => search.setDraft(event.target.value)}
      onCompositionStart={() => search.setComposing(true)} onCompositionEnd={() => search.setComposing(false)} />
    {search.draft && <button type="button" className="icon-button" aria-label="Clear search" onClick={search.clear}>×</button>}
  </form>;
}

export function ChatSearchPanel({ children, onOpen, search }: {
  children: ReactNode; onOpen: (id: string) => void; search: SearchState;
}) {
  return <section className="sidebar-chat-search" aria-label="Chat history" onKeyDown={search.onKeyDown}>
    <div className="chat-browse" hidden={!!search.keyword}>{children}</div>
    {search.resultQuery && <div className="chat-search-matches" ref={search.matches}>
      <Matches key={search.resultQuery} query={search.resultQuery} onSelect={onOpen} />
    </div>}
  </section>;
}
