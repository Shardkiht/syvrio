import { useEffect, useLayoutEffect, useRef, useState, type MutableRefObject, type RefObject, type KeyboardEvent } from 'react';
import { isCommandLine, suggestionsFor, type Suggestion } from '../state/commands';
import { isComposing } from '../state/shortcuts';
import type { CommandInfo, CompactionPhase, ContextUsage } from '../types';
import { ContextMeter, type MeterReading } from './ContextMeter';
import { CommandSuggestions } from './CommandUI';
import { ArrowUpIcon, FileIcon, PlusIcon, StopIcon } from './Icons';

/** Starter commands shown under the home composer, in display order. */
const STARTER_COMMANDS = ['model', 'status', 'help', 'sessions'];
export type ComposerDraft = { text: string; files: File[] };

export function Composer({
  sessionId = null,
  drafts,
  busy,
  checkingSelection = false,
  onSendAfterSelection,
  progress,
  contextUsage,
  meterReading,
  compaction = null,
  pickerRef,
  openFiles,
  onSend,
  onInsert,
  queued = [],
  queuePaused = false,
  onRemoveQueued,
  undoDraft,
  onUndoDraftApplied,
  onStop,
  catalog = [],
  focusSignal = 0,
  hero = false,
}: {
  sessionId?: string | null;
  drafts?: Map<string | null, ComposerDraft>;
  busy: boolean;
  checkingSelection?: boolean;
  onSendAfterSelection?: (text: string, files: readonly File[], handedOff: () => void) => Promise<void>;
  progress: string;
  contextUsage?: ContextUsage | null;
  meterReading?: MutableRefObject<MeterReading | null>;
  compaction?: CompactionPhase | null;
  pickerRef?: RefObject<HTMLInputElement>;
  openFiles?: () => void;
  onSend: (text: string, files?: File[]) => void;
  onInsert?: (text: string) => Promise<boolean>;
  queued?: string[];
  queuePaused?: boolean;
  onRemoveQueued?: (index: number) => void;
  undoDraft?: string;
  onUndoDraftApplied?: () => void;
  onStop: () => void;
  catalog?: CommandInfo[];
  focusSignal?: number;
  /** Centred home layout: narrower card plus a row of starter commands. */
  hero?: boolean;
}) {
  const [value, setValue] = useState(() => drafts?.get(sessionId)?.text ?? '');
  const [files, setFiles] = useState<File[]>(() => drafts?.get(sessionId)?.files ?? []);
  const previousId = useRef(sessionId);
  useLayoutEffect(() => {
    if (!drafts) return;
    // The same composer stays mounted when its first send gets a session ID,
    // or when an archive moves its unfinished input back to the empty view.
    if (previousId.current !== sessionId) drafts.delete(previousId.current);
    previousId.current = sessionId;
    if (value || files.length) drafts.set(sessionId, { text: value, files });
    else drafts.delete(sessionId);
  }, [drafts, files, sessionId, value]);
  const [fileError, setFileError] = useState('');
  const localPicker = useRef<HTMLInputElement>(null);
  const picker = pickerRef ?? localPicker;
  const [inserting, setInserting] = useState<string | null>(null);
  const deferredSend = useRef(false);
  const [waitingToSend, setWaitingToSend] = useState(false);
  const [index, setIndex] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const lastFocusSignal = useRef(focusSignal);

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
    if (focusSignal !== lastFocusSignal.current) {
      lastFocusSignal.current = focusSignal;
      ref.current?.focus({ preventScroll: true });
    }
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
    if (deferredSend.current) return;
    const text = value.trim() || (files.length ? 'Analyze the attached files.' : '');
    if (!text) return;
    if (text === inserting) return;
    if (checkingSelection) {
      if (!onSendAfterSelection || isCommandLine(text)) return;
      deferredSend.current = true;
      setWaitingToSend(true);
      let handedOff = false;
      const release = () => { deferredSend.current = false; setWaitingToSend(false); };
      void onSendAfterSelection(text, files, () => {
        handedOff = true;
        drafts?.delete(sessionId);
        setValue('');
        setFiles([]);
        setFileError('');
        release();
      }).finally(() => {
        if (!handedOff) release(); // A failed or superseded bind leaves this draft intact.
      });
      return;
    }
    if (files.length && !isCommandLine(text)) {
      onSend(text, files);
      setFiles([]);
      setFileError('');
    } else onSend(text);
    setValue('');
    setDismissed(false);
    setIndex(0);
  };

  const insert = async () => {
    if (checkingSelection) return;
    if (files.length) {
      setFileError('Press Enter to send these attachments with the next message.');
      return;
    }
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
    if (checkingSelection && event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (!event.ctrlKey && !event.metaKey) submit();
      return;
    }
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

  const hasDraft = Boolean(value.trim() || files.length);
  const commandDraft = isCommandLine(value);

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
                <button type="button" disabled={checkingSelection} aria-label={`Remove queued message ${index + 1}`}
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
          <input ref={picker} type="file" multiple hidden disabled={waitingToSend} aria-label="Select attachments"
            accept=".md,.markdown,.txt,.text,.log,.json,.jsonl,.ndjson,.csv,.tsv,.yaml,.yml,.xml,.pdf,.docx,.xlsx,.xls,.pptx,.html,.htm,.epub,.msg,.ipynb,.png,.jpg,.jpeg,.webp,.gif"
            onChange={event => {
              const next = [...files, ...Array.from(event.target.files ?? [])];
              event.target.value = '';
              if (next.length > 8 || next.some(file => !file.size || file.size > 20 * 1024 * 1024)
                || next.reduce((sum, file) => sum + file.size, 0) > 40 * 1024 * 1024) {
                setFileError('Choose up to 8 non-empty files, 20 MiB each and 40 MiB total.');
              } else { setFiles(next); setFileError(''); }
            }} />
          {files.length > 0 && (
            <ul className="attachment-list" aria-label="Attachments">
              {files.map((file, index) => {
                const extension = file.name.match(/^.+(\.[a-z0-9]{1,10})$/i)?.[1] ?? '';
                const stem = file.name.slice(0, file.name.length - extension.length);
                return (
                  <li className="attachment-preview" key={index}>
                    <FileIcon size={18} />
                    <span className="attachment-name" title={file.name}>
                      <bdi className="attachment-stem">{stem}</bdi>{extension}
                    </span>
                    <button type="button" className="attachment-remove" disabled={waitingToSend}
                      aria-label={`Remove ${file.name}`}
                      onClick={() => setFiles(current => current.filter((_, position) => position !== index))}>
                      <span aria-hidden="true">×</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {fileError && <div role="alert">{fileError}</div>}
          <textarea
            ref={ref}
            value={value}
            readOnly={waitingToSend}
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
            <button type="button" className="icon-button composer-attach"
              disabled={waitingToSend} aria-label="Attach files" title="Attach files" onClick={openFiles ?? (() => picker.current?.click())}>
              <PlusIcon size={18} />
            </button>
            <div className="composer-actions">
              {busy && commandDraft && (
                <button type="button" className="composer-secondary" disabled={checkingSelection} onClick={submit}>Run</button>
              )}
              {busy && hasDraft && !commandDraft && (
                <>
                  {onInsert && !files.length && (
                    <button type="button" className="composer-secondary" onClick={() => void insert()}
                      title="Insert at the next safe boundary (Ctrl+Enter)"
                      disabled={checkingSelection || inserting !== null}>{inserting ? 'Waiting…' : 'Insert'}</button>
                  )}
                  <button type="button" className="composer-secondary" onClick={submit}
                    disabled={checkingSelection || inserting === value.trim()} aria-label="Queue message"
                    title="Send after the current reply (Enter)">Queue</button>
                </>
              )}
              <ContextMeter reading={meterReading} usage={contextUsage} compaction={compaction} />
              <button type="button" className={busy ? 'stop' : 'send'} onClick={busy ? onStop : submit}
                disabled={waitingToSend || (checkingSelection && (!onSendAfterSelection || busy || commandDraft)) || (!busy && !hasDraft)}
                aria-label={busy ? 'Stop' : 'Send'} title={busy ? 'Stop generating' : 'Send'}>
                {busy ? <StopIcon size={20} /> : <ArrowUpIcon size={23} />}
              </button>
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
      <div className="composer-hint">{waitingToSend ? 'Waiting to send…' : 'Enter to send or queue · Ctrl+Enter to insert'}</div>
    </div>
  );
}
