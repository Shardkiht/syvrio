import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { isCommandLine, suggestionsFor, type Suggestion } from '../state/commands';
import { isComposing } from '../state/shortcuts';
import type { CommandInfo } from '../types';
import { CommandSuggestions } from './CommandUI';
import { ArrowUpIcon, PlusIcon, StopIcon } from './Icons';

/** Starter commands shown under the home composer, in display order. */
const STARTER_COMMANDS = ['model', 'status', 'help', 'sessions'];

export function Composer({
  busy,
  progress,
  onSend,
  onStop,
  catalog = [],
  focusSignal = 0,
  hero = false,
  onOpenCommands,
}: {
  busy: boolean;
  progress: string;
  onSend: (text: string) => void;
  onStop: () => void;
  catalog?: CommandInfo[];
  focusSignal?: number;
  /** Centred home layout: narrower card plus a row of starter commands. */
  hero?: boolean;
  onOpenCommands?: () => void;
}) {
  const [value, setValue] = useState('');
  const [index, setIndex] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  // `rows={3}` gives the textarea its intrinsic three-line height; this effect
  // only grows it past that. `overflow-y` is driven by the same measurement so
  // an idle composer never shows a scrollbar: with border-box, setting
  // height = scrollHeight drops the padding and leaves a 1-2px overflow, which
  // is what produced the permanent scrollbar track.
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const fit = () => {
      element.style.height = '';
      const borders = element.offsetHeight - element.clientHeight;
      const needed = element.scrollHeight + borders;
      const style = window.getComputedStyle(element);
      const max = Number.parseFloat(style.maxHeight) || 200;
      // +1 absorbs sub-pixel line-height rounding; without it the box would
      // creep one pixel taller on every keystroke.
      if (needed > element.offsetHeight + 1) {
        element.style.height = `${Math.min(needed, max)}px`;
      }
      element.style.overflowY = needed > max ? 'auto' : 'hidden';
    };
    fit();
    // Wrapping changes when the pane width changes (sidebar toggle, rotation),
    // but our own height writes must not re-trigger the observer.
    let width = element.clientWidth;
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      if (element.clientWidth === width) return;
      width = element.clientWidth;
      fit();
    });
    observer?.observe(element);
    return () => observer?.disconnect();
  }, [value]);

  useEffect(() => {
    if (focusSignal > 0) ref.current?.focus();
  }, [focusSignal]);

  const suggestions = suggestionsFor(value, catalog);
  const showSuggestions = !dismissed && suggestions.length > 0;
  const active = suggestions[Math.min(index, Math.max(0, suggestions.length - 1))];

  const applyCompletion = (suggestion: Suggestion) => {
    setValue(suggestion.insert);
    setIndex(0);
    setDismissed(false);
    ref.current?.focus();
    ref.current?.setSelectionRange(suggestion.insert.length, suggestion.insert.length);
  };

  const submit = () => {
    const text = value.trim();
    if (!text) return;
    // Read-only slash commands stay available while a reply streams; the
    // backend decides which ones are safe instead of freezing the composer.
    if (busy && !isCommandLine(text)) return;
    onSend(text);
    setValue('');
    setDismissed(false);
    setIndex(0);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // A Chinese/Japanese IME composition must never trigger shortcuts.
    if (isComposing(event)) return;
    if (showSuggestions) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        setIndex((current) => Math.min(current + 1, suggestions.length - 1));
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        setIndex((current) => Math.max(current - 1, 0));
        return;
      }
      if (event.key === 'Tab') {
        event.preventDefault();
        if (active) applyCompletion(active);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        setDismissed(true);
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey && active) {
        event.preventDefault();
        setValue('');
        setDismissed(false);
        setIndex(0);
        onSend(active.insert);
        return;
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  const starters = hero
    ? STARTER_COMMANDS.flatMap((name) => {
        const found = catalog.find((item) => item.name === name);
        return found ? [found] : [];
      })
    : [];

  const fillCommand = (name: string) => {
    setValue(`/${name} `);
    setDismissed(false);
    setIndex(0);
    ref.current?.focus();
  };

  return (
    <div className={`composer${hero ? ' composer-hero' : ''}`}>
      <CommandSuggestions
        suggestions={showSuggestions ? suggestions : []}
        index={Math.min(index, Math.max(0, suggestions.length - 1))}
        onHover={setIndex}
        onPick={applyCompletion}
      />
      {busy && <div className="progress" data-testid="progress">{progress || 'Working...'}</div>}
      <div className="composer-card">
        <textarea
          ref={ref}
          value={value}
          rows={3}
          aria-label="Message"
          data-testid="composer-input"
          placeholder="Message Syvrio"
          onChange={(event) => {
            setValue(event.target.value);
            setDismissed(false);
            setIndex(0);
          }}
          onKeyDown={onKeyDown}
        />
        <div className="composer-bar">
          {onOpenCommands && (
            <button
              type="button"
              className="icon-button composer-plus"
              onClick={onOpenCommands}
              aria-label="Insert command"
              title="Commands (Alt+P)"
            >
              <PlusIcon size={17} />
            </button>
          )}
          <div className="composer-actions">
            {busy ? (
              <>
                {isCommandLine(value) && (
                  <button type="button" className="send run-command" onClick={submit}>Run</button>
                )}
                <button type="button" className="stop" onClick={onStop} aria-label="Stop">
                  <StopIcon size={16} />
                </button>
              </>
            ) : (
              <button
                type="button"
                className="send"
                onClick={submit}
                disabled={!value.trim()}
                aria-label="Send"
              >
                <ArrowUpIcon size={18} />
              </button>
            )}
          </div>
        </div>
      </div>
      {starters.length > 0 && (
        <div className="composer-chips">
          {starters.map((item) => (
            <button key={item.name} type="button" onClick={() => fillCommand(item.name)}>
              /{item.name}
            </button>
          ))}
        </div>
      )}
      <div className="composer-hint">Enter to send · Shift+Enter for a new line · / for commands</div>
    </div>
  );
}
