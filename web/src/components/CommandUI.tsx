import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { searchCommands, type ResultAction, type ResultPanel, type Suggestion } from '../state/commands';
import { isComposing } from '../state/shortcuts';
import type { CommandInfo, SessionInfo } from '../types';
import { CheckIcon } from './Icons';
import { Markdown } from './Markdown';
import './command-results.css';

export function CommandDialog({
  title, className, testId, role = 'dialog', overlayClass = '', onClose, onKeyDown, children,
}: {
  title: string;
  className: string;
  testId?: string;
  role?: 'dialog' | 'alertdialog';
  overlayClass?: string;
  onClose: () => void;
  onKeyDown?: (event: KeyboardEvent<HTMLDivElement>) => void;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = dialog.current;
    if (node && !node.contains(document.activeElement)) node.focus();
  }, []);
  return (
    <div className={`overlay ${overlayClass}`} data-testid={testId} onClick={onClose}>
      <div className={className} role={role} aria-label={title} aria-modal="true"
        tabIndex={-1} ref={dialog} onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (isComposing(event)) return;
          onKeyDown?.(event);
          if (event.defaultPrevented) return;
          if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            onClose();
          } else if (event.key === 'Tab') {
            const items = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(
              'button:not([disabled]):not([tabindex="-1"]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex="0"]',
            )).filter((node) => !node.closest('[hidden]'));
            const first = items[0];
            const last = items.at(-1);
            if (!first || event.target === event.currentTarget || event.target === (event.shiftKey ? first : last)) {
              event.preventDefault();
              (event.shiftKey ? last : first)?.focus();
            }
          }
        }}>
        {children}
      </div>
    </div>
  );
}

export function moveOption(event: KeyboardEvent, buttons: HTMLButtonElement[]): void {
  if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
  if (!buttons.length) return;
  event.preventDefault();
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const next = current < 0
    ? (event.key === 'ArrowDown' ? 0 : buttons.length - 1)
    : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
  buttons[next].focus();
}

/** Prefix/child completions anchored just above the composer. */
export function CommandSuggestions({
  suggestions,
  index,
  onPick,
}: {
  suggestions: Suggestion[];
  index: number;
  onPick: (suggestion: Suggestion) => void;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const selectedKey = suggestions[index]?.key;
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')
      ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [selectedKey]);

  if (!suggestions.length) return null;
  return (
    <div ref={listRef} className="command-suggestions" data-testid="command-suggestions" role="listbox">
      {suggestions.map((suggestion, position) => (
        <button
          key={suggestion.key}
          type="button"
          role="option"
          aria-selected={position === index}
          className={`command-suggestion${position === index ? ' active' : ''}`}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => onPick(suggestion)}
        >
          <span className="command-suggestion-label">{suggestion.label}</span>
          <span className="command-suggestion-desc">{suggestion.description}</span>
        </button>
      ))}
    </div>
  );
}

/** Lightweight palette over the same catalog, with its own keyboard handling. */
export function CommandPalette({
  catalog,
  onRun,
  onClose,
}: {
  catalog: CommandInfo[];
  onRun: (line: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState('');
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const matches = searchCommands(query, catalog);
  const selected = matches[Math.max(0, Math.min(index, matches.length - 1))];
  const selectedName = selected?.name;

  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')
      ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [selectedName]);

  return (
    <CommandDialog title="Command palette" className="command-palette"
      testId="command-palette-overlay" onClose={onClose}>
        <input
          ref={inputRef}
          data-testid="palette-input"
          className="palette-input"
          placeholder="Search commands"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setIndex(0);
          }}
          onKeyDown={(event) => {
            if (isComposing(event)) return;
            if (event.key === 'ArrowDown' && matches.length > 0) {
              event.preventDefault();
              setIndex((value) => Math.min(value + 1, matches.length - 1));
            } else if (event.key === 'ArrowUp') {
              event.preventDefault();
              setIndex((value) => Math.max(value - 1, 0));
            } else if (event.key === 'Enter') {
              event.preventDefault();
              if (selected) onRun(`/${selected.name}`);
            }
          }}
        />
        <div ref={listRef} className="palette-list" role="listbox">
          {matches.map((command, position) => (
            <button
              key={command.name}
              type="button"
              role="option"
              aria-selected={command.name === selectedName}
              className={`palette-row${command.name === selectedName ? ' active' : ''}`}
              onMouseEnter={() => setIndex(position)}
              onClick={() => onRun(`/${command.name}`)}
            >
              <span className="palette-usage">/{command.name}</span>
              <span className="palette-description">{command.description}</span>
              {command.support === 'unsupported' && (
                <span className="palette-note">unsupported</span>
              )}
            </button>
          ))}
          {!matches.length && <div className="palette-empty">No matching commands</div>}
        </div>
    </CommandDialog>
  );
}

/** Server-required confirmation; the backend re-checks independently. */
export function ConfirmDialog({
  message,
  onConfirm,
  onCancel,
  title = 'Confirm',
  confirmLabel = 'Confirm',
  confirmDisabled = false,
  focusCancel = false,
  children,
  testId = 'confirm-overlay',
}: {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  title?: string;
  confirmLabel?: string;
  confirmDisabled?: boolean;
  focusCancel?: boolean;
  children?: ReactNode;
  testId?: string;
}) {
  return (
    <CommandDialog title={title} className="confirm-dialog" testId={testId} role="alertdialog"
      onClose={onCancel} onKeyDown={(event) => {
        if (!focusCancel && !confirmDisabled && event.key === 'Enter' && event.target === event.currentTarget) {
          event.preventDefault();
          onConfirm();
        }
      }}>
        <div className="confirm-title">{title}</div>
        <div className="confirm-message" data-testid="confirm-message">{message}</div>
        {children}
        <div className="confirm-actions">
          <button type="button" autoFocus={focusCancel} onClick={onCancel}>Cancel</button>
          <button type="button" className="confirm-ok" disabled={confirmDisabled} onClick={onConfirm}>{confirmLabel}</button>
        </div>
    </CommandDialog>
  );
}

export function CommandResultDialog({ panel, title, onRun, onClose }: {
  panel: ResultPanel;
  title: string;
  onRun: (line: string) => void | Promise<unknown>;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState(panel.draft ?? title);
  const [working, setWorking] = useState<string | null>(null);
  const [refreshSource, setRefreshSource] = useState<ResultPanel | null>(null);
  const [actionError, setActionError] = useState('');
  const body = useRef<HTMLDivElement>(null);
  const actions = panel.rename ? [] : panel.actions ?? [];
  const shortcut = actions.find((action) => action.saveShortcut);
  const saveLine = panel.rename && draft.trim() ? `/rename ${draft.trim()}` : shortcut?.line;
  const run = async (line: string | undefined, refresh = false) => {
    if (working !== null || !line) return;
    setWorking(line);
    setRefreshSource(refresh ? panel : null);
    setActionError('');
    try { await onRun(line); }
    catch (error) { setActionError(error instanceof Error ? error.message : 'The command failed. Try again.'); }
    finally { setWorking(null); }
  };
  const grouped = Boolean(panel.groups?.length);
  const receipt = !grouped && !panel.reading && !panel.rename;
  const inlineFields = new Set(panel.groups?.flatMap((group) => group.fields.map(({ label }) => label)));
  const actionButton = (action: ResultAction, inline = false) => (
    <button type="button" key={action.line}
      className={inline ? 'result-field-button' : action.primary ? 'result-primary' : undefined}
      disabled={working !== null} aria-busy={working === action.line}
      onClick={() => void run(action.line, action.refresh)}>
      {working === action.line ? 'Working…' : action.label}
    </button>
  );
  return (
    <CommandDialog title={panel.title} className={`command-panel command-result result-sheet${receipt ? ' result-receipt' : ''}`}
      testId="command-result-overlay" onClose={onClose} onKeyDown={(event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's'
          && (panel.rename || shortcut)) {
          event.preventDefault();
          void run(saveLine);
        } else if (body.current && (event.key === 'PageUp' || event.key === 'PageDown')) {
          event.preventDefault();
          body.current.scrollBy?.({ top: body.current.clientHeight * (event.key === 'PageUp' ? -1 : 1) });
        } else if (event.key === 'Enter' && (event.target === event.currentTarget || panel.rename
          && event.target instanceof HTMLInputElement)) {
          event.preventDefault();
          if (panel.rename) void run(saveLine);
          else onClose();
        }
      }}>
      <header className="result-header">
        {receipt && panel.feedback && <span className="result-symbol" data-feedback={panel.feedback} aria-hidden="true">
          {panel.feedback === 'success' ? <CheckIcon size={15} /> : panel.feedback === 'error' ? '!' : 'i'}
        </span>}
        <h2>{panel.title}</h2>
      </header>
      {panel.metadata && panel.metadata.length > 0 &&
        <ul className="result-metadata">{panel.metadata.map((item) => <li key={item}>{item}</li>)}</ul>}
      {panel.rename ? (
        <input aria-label="Session title" className="palette-input" autoFocus readOnly={working !== null}
          value={draft} onChange={(event) => setDraft(event.target.value)} />
      ) : (panel.message || grouped) && <div ref={body} className="result-scroll" tabIndex={0}>
        {grouped ? panel.groups?.map((group) => <section className="result-group" key={group.title}>
          <h3>{group.title}</h3>
          <dl>{group.fields.map(({ label, value }) => {
            const action = actions.find((item) => item.field === label);
            return <div key={label}><dt>{label}</dt><dd>
              <span>{value}</span>{action && actionButton(action, true)}
            </dd></div>;
          })}</dl>
        </section>) : panel.reading
          ? <article className="result-article"><Markdown text={panel.message} reading /></article>
          : <div className="result-text" role={panel.feedback === 'error' ? 'alert' : 'status'}>{panel.message}</div>}
      </div>}
      {panel.rename && panel.message && <p className="result-error" role="alert">{panel.message}</p>}
      {actionError && <p className="result-error" role="alert">{actionError}</p>}
      <div className="confirm-actions result-actions">
        {refreshSource && refreshSource !== panel && <span className="result-updated" role="status">Refreshed</span>}
        <button type="button" onClick={onClose}>{panel.rename ? 'Cancel' : 'Close'}</button>
        {actions.filter((action) => !action.field || !inlineFields.has(action.field)).map((action) => actionButton(action))}
        {panel.rename && <button type="button" className="result-primary" onClick={() => void run(saveLine)}
          disabled={working !== null || !draft.trim()} aria-busy={working !== null}>
          {working !== null ? 'Saving…' : 'Save'}
        </button>}
      </div>
    </CommandDialog>
  );
}

export function SessionCommandDialog({ sessions, currentId, onSelect, onNew, onArchive, onClose }: {
  sessions: SessionInfo[];
  currentId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onArchive: () => void;
  onClose: () => void;
}) {
  return (
    <CommandDialog title="Sessions" className="command-panel" onClose={onClose}
      onKeyDown={(event) => {
        const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('.panel-options button'));
        moveOption(event, buttons);
        if (event.key === 'Enter' && event.target === event.currentTarget) {
          event.preventDefault();
          (buttons.find((item) => item.getAttribute('aria-current') === 'true') ?? buttons[0])?.click();
        }
      }}>
      <div className="confirm-title">Sessions</div>
      <div className="panel-options">
        {sessions.map((session) => (
          <button key={session.id} type="button" aria-current={session.id === currentId}
            onClick={() => onSelect(session.id)}>
            {session.title}
          </button>
        ))}
        {!sessions.length && <div className="command-result-body">No conversations yet</div>}
      </div>
      <div className="confirm-actions">
        <button type="button" onClick={onNew}>New chat</button>
        <button type="button" onClick={onArchive}>Browse archive</button>
        <button type="button" onClick={onClose}>Close</button>
      </div>
    </CommandDialog>
  );
}
