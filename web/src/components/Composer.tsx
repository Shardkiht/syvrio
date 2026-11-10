import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { isCommandLine, suggestionsFor, type Suggestion } from '../state/commands';
import { isComposing } from '../state/shortcuts';
import type { CommandInfo } from '../types';
import { CommandSuggestions } from './CommandUI';

export function Composer({
  busy,
  progress,
  onSend,
  onStop,
  catalog = [],
  focusSignal = 0,
}: {
  busy: boolean;
  progress: string;
  onSend: (text: string) => void;
  onStop: () => void;
  catalog?: CommandInfo[];
  focusSignal?: number;
}) {
  const [value, setValue] = useState('');
  const [index, setIndex] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    element.style.height = 'auto';
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`;
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

  return (
    <div className="composer">
      <CommandSuggestions
        suggestions={showSuggestions ? suggestions : []}
        index={Math.min(index, Math.max(0, suggestions.length - 1))}
        onHover={setIndex}
        onPick={applyCompletion}
      />
      {busy && <div className="progress" data-testid="progress">{progress || 'Working...'}</div>}
      <div className="composer-row">
        <textarea
          ref={ref}
          value={value}
          rows={1}
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
        {busy ? (
          <>
            {isCommandLine(value) && (
              <button type="button" className="send run-command" onClick={submit}>Run</button>
            )}
            <button type="button" className="stop" onClick={onStop}>Stop</button>
          </>
        ) : (
          <button type="button" className="send" onClick={submit} disabled={!value.trim()}>Send</button>
        )}
      </div>
      <div className="composer-hint">Enter to send · Shift+Enter for a new line · / for commands</div>
    </div>
  );
}
