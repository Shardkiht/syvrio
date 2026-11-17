import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import { isCommandLine, suggestionsFor, type Suggestion } from '../state/commands';
import { isComposing } from '../state/shortcuts';
import type { CommandInfo, CompactionPhase, ContextUsage } from '../types';
import { CommandSuggestions } from './CommandUI';
import { ArrowUpIcon, PlusIcon, StopIcon } from './Icons';

/** Starter commands shown under the home composer, in display order. */
const STARTER_COMMANDS = ['model', 'status', 'help', 'sessions'];

const COMPACTION_LABEL = {
  running: 'Compressing context…',
  done: 'Context compressed',
  failed: 'Context compression failed',
};

function ContextMeter({ usage, compaction }: { usage: ContextUsage | null; compaction: CompactionPhase | null }) {
  const [open, setOpen] = useState(false);
  const ratio = usage && usage.limit > 0 ? usage.used / usage.limit : 0;
  const percent = Math.min(100, ratio * 100);
  const label = usage ? (percent > 0 && percent < 1 ? '<1%' : `${Math.floor(percent)}%`) : '—';
  const nearLimit = usage !== null && ratio >= usage.compact_threshold;
  const waveCeiling = compaction === 'running' ? 99 : 85;
  const detail = usage
    ? `${usage.used.toLocaleString()} / ${usage.limit.toLocaleString()} tokens`
    : 'Estimate unavailable';
  return (
    <div className={`context-meter${open ? ' expanded' : ''}`} data-compaction={compaction ?? undefined}>
      <button type="button" className="context-dial" aria-expanded={open}
        aria-label={`Context estimate: ${detail}`}
        data-fill={percent === 0 ? 'empty' : percent === 100 ? 'full' : 'partial'}
        data-wave={percent >= 10 && percent <= waveCeiling ? 'moving' : 'still'}
        style={{ '--context-fill': `${percent}%` } as CSSProperties}
        onClick={() => setOpen((value) => !value)}
        onBlur={() => setOpen(false)}
        onKeyDown={(event) => {
          if (!open || event.key !== 'Escape' || isComposing(event)) return;
          event.preventDefault();
          event.stopPropagation();
          setOpen(false);
        }}>
        <span className="context-value">{label}</span>
      </button>
      <div className="context-detail" aria-hidden="true">
        <strong>{compaction ? COMPACTION_LABEL[compaction] : 'Context estimate'}</strong>
        <span>{detail}</span>
        {nearLimit && <span className="context-warning">Near the compaction threshold</span>}
      </div>
      {compaction && <span className="sr-only" role="status">{COMPACTION_LABEL[compaction]}</span>}
    </div>
  );
}

export function Composer({
  busy,
  progress,
  contextUsage = null,
  compaction = null,
  onSend,
  onInsert,
  queued = [],
  queuePaused = false,
  onRemoveQueued,
  undoDraft,
  onUndo,
  undoing = false,
  onUndoDraftApplied,
  onStop,
  catalog = [],
  focusSignal = 0,
  hero = false,
  onOpenCommands,
}: {
  busy: boolean;
  progress: string;
  contextUsage?: ContextUsage | null;
  compaction?: CompactionPhase | null;
  onSend: (text: string) => void;
  onInsert?: (text: string) => Promise<boolean>;
  queued?: string[];
  queuePaused?: boolean;
  onRemoveQueued?: (index: number) => void;
  undoDraft?: string;
  onUndo?: () => void;
  undoing?: boolean;
  onUndoDraftApplied?: () => void;
  onStop: () => void;
  catalog?: CommandInfo[];
  focusSignal?: number;
  /** Centred home layout: narrower card plus a row of starter commands. */
  hero?: boolean;
  onOpenCommands?: () => void;
}) {
  const [value, setValue] = useState('');
  const [inserting, setInserting] = useState<string | null>(null);
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

  useEffect(() => {
    if (undoDraft === undefined || value.trim()) return;
    setValue(undoDraft);
    onUndoDraftApplied?.();
  }, [undoDraft, value, onUndoDraftApplied]);

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
    if (text === inserting) return;
    onSend(text);
    setValue('');
    setDismissed(false);
    setIndex(0);
  };

  const insert = async () => {
    const text = value.trim();
    if (!text || isCommandLine(text) || inserting !== null || !onInsert) return;
    setInserting(text);
    try {
      if (await onInsert(text)) setValue((draft) => draft.trim() === text ? '' : draft);
    } finally {
      setInserting(null);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // A Chinese/Japanese IME composition must never trigger shortcuts.
    if (isComposing(event)) return;
    if (event.key === 'Enter' && event.ctrlKey && !event.shiftKey && !isCommandLine(value)) {
      event.preventDefault();
      if (busy) void insert();
      else submit();
      return;
    }
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
      {busy && <div className="progress" data-testid="progress">{progress || 'Working...'}</div>}
      {queued.length > 0 && (
        <section className="web-send-queue" aria-label="Send queue">
          <div role="status">{queued.length} queued{queuePaused ? ' · paused until you send again' : ''}</div>
          <ol>
            {queued.map((text, index) => (
              <li key={index}>
                <span title={text}>{text}</span>
                <button type="button" aria-label={`Remove queued message ${index + 1}`}
                  onClick={() => onRemoveQueued?.(index)}>×</button>
              </li>
            ))}
          </ol>
        </section>
      )}
      <div className="composer-lane">
        <CommandSuggestions
          suggestions={showSuggestions ? suggestions : []}
          index={Math.min(index, Math.max(0, suggestions.length - 1))}
          onHover={setIndex}
          onPick={applyCompletion}
        />
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
              {busy && isCommandLine(value) && (
                <button type="button" className="send run-command" onClick={submit}>Run</button>
              )}
              <ContextMeter usage={contextUsage} compaction={compaction} />
              {onUndo && (
                <button type="button" className="send run-command" onClick={onUndo}
                  disabled={undoing} title="Undo the last turn and restore its input">
                  {undoing ? 'Undoing…' : 'Undo'}
                </button>
              )}
              {busy ? (
                <>
                  {!isCommandLine(value) && (
                    <>
                      <button type="button" className="send run-command" onClick={() => void insert()}
                        title="Insert at the next safe boundary (Ctrl+Enter)"
                        disabled={!value.trim() || inserting !== null}>{inserting ? 'Waiting…' : 'Insert'}</button>
                      <button type="button" className="send" onClick={submit}
                        disabled={!value.trim() || inserting === value.trim()} aria-label="Queue message">
                        <ArrowUpIcon size={23} />
                      </button>
                    </>
                  )}
                  <button type="button" className="stop" onClick={onStop} aria-label="Stop">
                    <StopIcon size={20} />
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
                  <ArrowUpIcon size={23} />
                </button>
              )}
            </div>
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
      <div className="composer-hint">Enter to send or queue · Ctrl+Enter to insert · Shift+Enter for a new line</div>
    </div>
  );
}
