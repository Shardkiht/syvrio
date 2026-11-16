import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { searchCommands, type OptionPanel, type ResultPanel, type Suggestion } from '../state/commands';
import { isComposing, SHORTCUT_HELP } from '../state/shortcuts';
import type { CommandInfo, SessionInfo } from '../types';

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
  onHover,
  onPick,
}: {
  suggestions: Suggestion[];
  index: number;
  onHover: (index: number) => void;
  onPick: (suggestion: Suggestion) => void;
}) {
  if (!suggestions.length) return null;
  return (
    <div className="command-suggestions" data-testid="command-suggestions" role="listbox">
      {suggestions.map((suggestion, position) => (
        <button
          key={suggestion.key}
          type="button"
          role="option"
          aria-selected={position === index}
          className={`command-suggestion${position === index ? ' active' : ''}`}
          onMouseDown={(event) => event.preventDefault()}
          onMouseEnter={() => onHover(position)}
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
  const matches = searchCommands(query, catalog);

  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => { setIndex(0); }, [query]);

  const selected = matches[Math.min(index, Math.max(0, matches.length - 1))];
  return (
    <CommandDialog title="Command palette" className="command-palette"
      testId="command-palette-overlay" onClose={onClose}>
        <input
          ref={inputRef}
          data-testid="palette-input"
          className="palette-input"
          placeholder="Search commands"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (isComposing(event)) return;
            if (event.key === 'ArrowDown') {
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
        <div className="palette-list" role="listbox">
          {matches.map((command, position) => (
            <button
              key={command.name}
              type="button"
              role="option"
              aria-selected={position === index}
              className={`palette-row${position === index ? ' active' : ''}`}
              onMouseEnter={() => setIndex(position)}
              onClick={() => onRun(`/${command.name}`)}
            >
              <span className="palette-usage">{command.usage}</span>
              <span className="palette-description">{command.description}</span>
              {command.shortcut && <span className="palette-shortcut">{command.shortcut}</span>}
              {command.support === 'unsupported' && (
                <span className="palette-note">unsupported</span>
              )}
            </button>
          ))}
          {!matches.length && <div className="palette-empty">No matching commands</div>}
        </div>
        <div className="palette-help">
          {SHORTCUT_HELP.map((item) => (
            <div key={item.keys} className="palette-help-row" title={item.note}>
              <span>{item.keys}</span>
              <span>{item.description}{item.note ? ` · ${item.note}` : ''}</span>
            </div>
          ))}
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
  testId = 'confirm-overlay',
}: {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  title?: string;
  confirmLabel?: string;
  testId?: string;
}) {
  return (
    <CommandDialog title={title} className="confirm-dialog" testId={testId} role="alertdialog"
      onClose={onCancel} onKeyDown={(event) => {
        if (event.key === 'Enter' && event.target === event.currentTarget) {
          event.preventDefault();
          onConfirm();
        }
      }}>
        <div className="confirm-title">{title}</div>
        <div className="confirm-message" data-testid="confirm-message">{message}</div>
        <div className="confirm-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button type="button" className="confirm-ok" onClick={onConfirm}>{confirmLabel}</button>
        </div>
    </CommandDialog>
  );
}

/** Clickable model / reasoning / context choices produced by a command. */
export function CommandPanelDialog({
  panel,
  onPick,
  onClose,
}: {
  panel: OptionPanel;
  onPick: (line: string) => void;
  onClose: () => void;
}) {
  return (
    <CommandDialog title={panel.title} className="command-panel" testId="command-panel-overlay"
      onClose={onClose} onKeyDown={(event) => {
        moveOption(event, Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('.panel-options button')));
        if (event.key === 'Enter' && event.target === event.currentTarget && panel.options[0]) {
          event.preventDefault();
          onPick(`${panel.prefix} ${panel.options[0].value}`);
        }
      }}>
      <div className="confirm-title">{panel.title}</div>
      {panel.message && <div className="command-result-body" tabIndex={0}>{panel.message}</div>}
      <div className="panel-options" data-testid="panel-options">
        {panel.options.map((option) => (
          <button key={option.value} type="button"
            onClick={() => onPick(`${panel.prefix} ${option.value}`)}>
            {option.label}
          </button>
        ))}
      </div>
      <div className="confirm-actions">
        <button type="button" onClick={onClose}>Close</button>
      </div>
    </CommandDialog>
  );
}

export function CommandResultDialog({ panel, title, onRun, onClose }: {
  panel: ResultPanel;
  title: string;
  onRun: (line: string) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState(title);
  const body = useRef<HTMLDivElement>(null);
  const save = () => {
    if (panel.rename && draft.trim()) onRun(`/rename ${draft.trim()}`);
    else if (panel.action) onRun(panel.action.line);
  };
  return (
    <CommandDialog title={panel.title} className="command-panel command-result"
      testId="command-result-overlay" onClose={onClose} onKeyDown={(event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's'
          && (panel.rename || panel.action?.saveShortcut)) {
          event.preventDefault();
          save();
        } else if (body.current && (event.key === 'PageUp' || event.key === 'PageDown')) {
          event.preventDefault();
          body.current.scrollBy?.({ top: body.current.clientHeight * (event.key === 'PageUp' ? -1 : 1) });
        } else if (event.key === 'Enter' && (event.target === event.currentTarget || panel.rename
          && event.target instanceof HTMLInputElement)) {
          event.preventDefault();
          if (panel.rename) save();
          else onClose();
        }
      }}>
      <div className="confirm-title">{panel.title}</div>
      {panel.rename ? (
        <input aria-label="Session title" className="palette-input" autoFocus maxLength={200}
          value={draft} onChange={(event) => setDraft(event.target.value)} />
      ) : panel.message && <div ref={body} className="command-result-body" tabIndex={0}>{panel.message}</div>}
      <div className="confirm-actions">
        <button type="button" onClick={onClose}>{panel.rename ? 'Cancel' : 'Close'}</button>
        {(panel.rename || panel.action) && (
          <button type="button" onClick={save} disabled={panel.rename && !draft.trim()}>
            {panel.rename ? 'Save' : panel.action?.label}
          </button>
        )}
      </div>
    </CommandDialog>
  );
}

export function SessionCommandDialog({ sessions, currentId, onSelect, onClose }: {
  sessions: SessionInfo[];
  currentId: string | null;
  onSelect: (id: string) => void;
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
      <div className="confirm-actions"><button type="button" onClick={onClose}>Close</button></div>
    </CommandDialog>
  );
}
