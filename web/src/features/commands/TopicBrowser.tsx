import { useLayoutEffect, useRef, useState } from 'react';
import { usePanelRead } from './usePanelRead';
import type { TopicDocument, TopicListItem } from './commands';
import { CommandDialog, moveOption } from './CommandUI';
import { ArrowUpIcon, FileIcon, SearchIcon } from '../../shared/Icons';
import './topics.css';

function logicalPath(topic: string) {
  return topic.replace(/^memories\//, '').replace(/\.md$/, '');
}

function topicLabels(topic: string, title?: string) {
  const path = logicalPath(topic);
  const slash = path.lastIndexOf('/');
  return {
    path,
    category: slash < 0 ? '' : path.slice(0, slash),
    title: title?.trim() || path.slice(slash + 1),
  };
}

function entryCount(n: number) {
  return n === 1 ? '1 entry' : `${n} entries`;
}

function ReadError({ label, detail, retry }: { label: string; detail: string; retry: () => void }) {
  return (
    <div className="topics-error">
      <p role="alert">{label}{detail && ` ${detail}`}</p>
      <button type="button" onClick={(event) => {
        event.currentTarget.closest<HTMLElement>('[role="dialog"]')?.focus();
        retry();
      }}>Retry</button>
    </div>
  );
}

function TopicReader({ topic, title, initial, readTopic }: {
  topic: string;
  title?: string;
  initial?: TopicDocument;
  readTopic: (topic: string) => Promise<TopicDocument>;
}) {
  const { value: doc, error, retry } = usePanelRead(initial, () => readTopic(topic));
  const labels = topicLabels(doc?.topic ?? topic, title);
  return (
    <>
      <header className="topics-reader-head">
        <h2>{labels.title}</h2>
        <div className="topics-byline" aria-live="polite" aria-atomic="true">
          {labels.category && <span>{labels.category}</span>}
          {doc && <span>{entryCount(doc.entries.length)}</span>}
        </div>
      </header>
      <div className="topics-scroll topics-reader-body" role="region" aria-label="Topic entries" tabIndex={0}>
        {error !== null ? <ReadError label="Couldn’t load this topic." detail={error} retry={retry} />
          : !doc ? <p className="topics-notice" role="status">Loading entries…</p>
          : !doc.entries.length ? <p className="topics-notice" role="status">No entries in this topic.</p>
          : <ul className="topics-entries">
            {doc.entries.map((entry) => (
              <li key={entry.id}>
                {entry.status && entry.status !== 'active' && <small>{entry.status}</small>}
                <p>{entry.text}</p>
              </li>
            ))}
          </ul>}
      </div>
    </>
  );
}

export function TopicBrowser({ initialTopics, initialDocument, readTopics, readTopic, onClose }: {
  initialTopics?: TopicListItem[];
  initialDocument?: TopicDocument;
  readTopics: () => Promise<TopicListItem[]>;
  readTopic: (topic: string) => Promise<TopicDocument>;
  onClose: () => void;
}) {
  const [directoryOpened, setDirectoryOpened] = useState(!initialDocument);
  const { value: topics, error, retry } = usePanelRead(initialTopics, readTopics, directoryOpened);
  const [selected, setSelected] = useState(initialDocument?.topic ?? null);
  const [filter, setFilter] = useState('');
  const back = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const lastRow = useRef<HTMLButtonElement | null>(null);
  const scrollPosition = useRef(0);
  useLayoutEffect(() => {
    if (selected !== null) {
      back.current?.focus({ preventScroll: true });
    } else {
      (lastRow.current ?? list.current?.closest<HTMLElement>('[role="dialog"]'))?.focus({ preventScroll: true });
      if (list.current) list.current.scrollTop = scrollPosition.current;
    }
  }, [selected]);

  const needle = filter.trim().toLowerCase();
  const groups = new Map<string, TopicListItem[]>();
  let matches = 0;
  for (const row of topics ?? []) {
    const { category, path, title } = topicLabels(row.topic, row.title);
    if (!title.toLowerCase().includes(needle) && !path.toLowerCase().includes(needle)) continue;
    const group = groups.get(category);
    if (group) group.push(row);
    else groups.set(category, [row]);
    matches++;
  }
  const selectedRow = selected === null ? undefined
    : topics?.find((row) => logicalPath(row.topic) === logicalPath(selected));
  const closeButton = <button type="button" className="icon-button topics-close" aria-label="Close topics"
    title="Close topics" onClick={onClose}>×</button>;

  return (
    <CommandDialog title="Topics" className="command-panel topics-browser" onClose={onClose}
      onKeyDown={(event) => {
        if (selected === null) {
          moveOption(event, Array.from(list.current?.querySelectorAll<HTMLButtonElement>('.topics-row') ?? []));
        }
      }}>
      <div className="topics-view" hidden={selected !== null}>
        <header className="topics-head">
          <div className="topics-title">
            <h2>Topics</h2>
            {topics && <span aria-live="polite">{needle ? `${matches} of ${topics.length}` : topics.length}</span>}
          </div>
          {closeButton}
        </header>
        {topics && topics.length > 0 && (
          <label className="review-search topics-search">
            <SearchIcon size={16} />
            <input type="search" aria-label="Filter topics" placeholder="Filter topics" value={filter}
              onChange={(event) => {
                setFilter(event.target.value);
                if (list.current) list.current.scrollTop = 0;
              }} />
          </label>
        )}
        <div ref={list} className="topics-scroll topics-catalog" role="region" aria-label="Topic list">
          {error !== null ? <ReadError label="Couldn’t load topics." detail={error} retry={retry} />
            : !topics ? <p className="topics-notice" role="status">Loading topics…</p>
            : !matches ? <p className="topics-notice" role="status">
              {topics.length ? 'No matching topics.' : 'No topics yet.'}
            </p>
            : Array.from(groups, ([category, rows]) => (
              <section className="topics-group" key={category}>
                {category && <h3>{category}</h3>}
                <ul>
                  {rows.map((row) => {
                    const { title, path } = topicLabels(row.topic, row.title);
                    return <li key={row.topic}><button type="button" className="topics-row" title={path}
                      onClick={(event) => {
                        lastRow.current = event.currentTarget;
                        scrollPosition.current = list.current?.scrollTop ?? 0;
                        setSelected(row.topic);
                      }}>
                      <FileIcon size={16} />
                      <span className="topics-row-title">{title}</span>
                      {row.entries !== undefined && <span className="topics-count">{entryCount(row.entries)}</span>}
                      <span className="topics-chevron" aria-hidden="true" />
                    </button></li>;
                  })}
                </ul>
              </section>
            ))}
        </div>
      </div>
      {selected !== null && <div className="topics-view">
        <div className="topics-head topics-nav">
          <button type="button" className="topics-back" ref={back} aria-label="Back to topics"
            onClick={() => { setDirectoryOpened(true); setSelected(null); }}><ArrowUpIcon size={16} />Topics</button>
          {closeButton}
        </div>
        <TopicReader key={selected} topic={selected} title={selectedRow?.title} readTopic={readTopic}
          initial={initialDocument && logicalPath(selected) === logicalPath(initialDocument.topic)
            ? initialDocument : undefined} />
      </div>}
    </CommandDialog>
  );
}
