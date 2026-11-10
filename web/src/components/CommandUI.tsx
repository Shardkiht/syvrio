import { useEffect, useRef, useState } from 'react';
import { searchCommands, type CommandPanel, type Suggestion } from '../state/commands';
import { isComposing, SHORTCUT_HELP } from '../state/shortcuts';
import type { CommandInfo } from '../types';

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
    <div className="overlay" data-testid="command-palette-overlay" onClick={onClose}>
      <div
        className="command-palette"
        role="dialog"
        aria-label="Command palette"
        onClick={(event) => event.stopPropagation()}
      >
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
            } else if (event.key === 'Escape') {
              event.preventDefault();
              event.stopPropagation();
              onClose();
            } else if (event.key === 'Tab') {
              event.preventDefault();
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
      </div>
    </div>
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
    <div className="overlay" data-testid={testId} onClick={onCancel}>
      <div
        className="confirm-dialog"
        role="alertdialog"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="confirm-title">{title}</div>
        <div className="confirm-message" data-testid="confirm-message">{message}</div>
        <div className="confirm-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button type="button" className="confirm-ok" onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

/** Clickable model / reasoning / context choices produced by a command. */
export function CommandPanelDialog({
  panel,
  onPick,
  onClose,
}: {
  panel: CommandPanel;
  onPick: (line: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="overlay" data-testid="command-panel-overlay" onClick={onClose}>
      <div
        className="command-panel"
        role="dialog"
        aria-label={panel.title}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="confirm-title">{panel.title}</div>
        <div className="panel-options" data-testid="panel-options">
          {panel.options.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => onPick(`${panel.prefix} ${option.value}`)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className="confirm-actions">
          <button type="button" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
