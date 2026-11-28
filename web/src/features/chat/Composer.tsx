import { useEffect, useLayoutEffect, useRef, useState, type MutableRefObject, type RefObject, type KeyboardEvent } from 'react';
import { canOpenDuringSelection, isCommandLine, suggestionsFor, type Suggestion } from '../commands/commands';
import { isComposing } from '../../shared/shortcuts';
import type { AttachmentPolicy, CommandInfo, CompactionPhase, ContextUsage } from '../../types';
import { ContextMeter, type MeterReading } from './ContextMeter';
import { CommandSuggestions } from '../commands/CommandUI';
import { SurfacePresence } from '../../shared/SurfacePresence';
import { clipboardFiles, uniqueDraftFiles } from './draftAttachments';
import { DraftAttachmentIcon } from './AttachmentImage';
import { PlusIcon, SendIcon, StopIcon } from '../../shared/Icons';

/** 首页输入框下的快捷命令，按显示顺序排列。 */
const STARTER_COMMANDS = ['model', 'status', 'help', 'sessions'];
export type ComposerDraft = { text: string; files: File[] };

export function Composer({
  sessionId = null,
  drafts,
  busy,
  checkingSelection = false,
  onSendAfterSelection,
  progress,
  newChat = false,
  contextUsage,
  meterReading,
  compaction = null,
  pickerRef,
  attachmentPolicy,
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
  onInputMount,
  hero = false,
}: {
  sessionId?: string | null;
  drafts?: Map<string | null, ComposerDraft>;
  busy: boolean;
  checkingSelection?: boolean;
  onSendAfterSelection?: (text: string, files: readonly File[], handedOff: () => void) => Promise<void>;
  progress: string;
  newChat?: boolean;
  contextUsage?: ContextUsage | null;
  meterReading?: MutableRefObject<MeterReading | null>;
  compaction?: CompactionPhase | null;
  attachmentPolicy?: AttachmentPolicy;
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
  onInputMount?: (input: HTMLTextAreaElement) => void;
  /** 首页使用居中布局，较窄的输入框下方是一行快捷命令。 */
  hero?: boolean;
}) {
  const [value, setValue] = useState(() => drafts?.get(sessionId)?.text ?? '');
  const [files, setFiles] = useState<File[]>(() => drafts?.get(sessionId)?.files ?? []);
  const currentFiles = useRef(files);
  useLayoutEffect(() => { currentFiles.current = files; }, [files]);
  const previousId = useRef(sessionId);
  useLayoutEffect(() => {
    if (!drafts) return;
    // 首次发送取得会话 ID，或归档时将未完成的输入移回空视图，均复用已挂载的输入框。
    if (previousId.current !== sessionId) drafts.delete(previousId.current);
    previousId.current = sessionId;
    if (value || files.length) drafts.set(sessionId, { text: value, files });
    else drafts.delete(sessionId);
  }, [drafts, files, sessionId, value]);
  const comparingFiles = useRef(false);
  const pendingSelections = useRef<File[][]>([]);
  const [checkingFiles, setCheckingFiles] = useState(false);
  const [fileError, setFileError] = useState('');
  const localPicker = useRef<HTMLInputElement>(null);
  const picker = pickerRef ?? localPicker;
  const [inserting, setInserting] = useState<string | null>(null);
  const deferredSend = useRef(false);
  const [waitingToSend, setWaitingToSend] = useState(false);
  const [index, setIndex] = useState(-1);
  const [commandHint, setCommandHint] = useState('');
  const [dismissed, setDismissed] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const lastFocusSignal = useRef(focusSignal);
  useLayoutEffect(() => {
    if (ref.current) onInputMount?.(ref.current);
  }, [onInputMount]);

  // 从当前布局的默认高度开始测量，内容放不下时再增高。
  // overflow-y 也由同一测量值控制，空闲输入框无需显示滚动条。
  // 在 border-box 下直接取 scrollHeight 作高度会漏算边距，留下 1 至 2 像素溢出，
  // 导致滚动条轨道一直可见。
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const fit = () => {
      element.style.height = '';
      const borders = element.offsetHeight - element.clientHeight;
      const needed = element.scrollHeight + borders;
      const style = window.getComputedStyle(element);
      const max = Number.parseFloat(style.maxHeight) || 200;
      // 加 1 用于吸收行高的亚像素舍入误差，避免每次按键都让输入框增高一个像素。
      if (needed > element.offsetHeight + 1) {
        element.style.height = `${Math.min(needed, max)}px`;
      }
      element.style.overflowY = needed > max ? 'auto' : 'hidden';
    };
    fit();
    // 侧栏开关或设备旋转会改变宽度和换行。只观察宽度，避免写入高度再次触发观察器。
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
  const active = suggestions[index];

  const applyCompletion = (suggestion: Suggestion) => {
    setValue(suggestion.insert);
    setCommandHint('');
    setIndex(-1);
    setDismissed(false);
    ref.current?.focus();
    ref.current?.setSelectionRange(suggestion.insert.length, suggestion.insert.length);
  };

  const addFiles = async (selection: File[]) => {
    if (deferredSend.current || !selection.length) return;
    if (!attachmentPolicy) {
      setFileError('Connect to the backend before attaching files.');
      return;
    }
    pendingSelections.current.push(selection);
    if (comparingFiles.current) return;
    comparingFiles.current = true;
    let draft = currentFiles.current;
    try {
      for (let next = pendingSelections.current.shift(); next; next = pendingSelections.current.shift()) {
        try {
          const result = uniqueDraftFiles([...draft, ...next], attachmentPolicy);
          if (Array.isArray(result)) draft = result;
          else {
            setCheckingFiles(true);
            draft = await result;
          }
          currentFiles.current = draft;
          setFiles(draft);
          setFileError('');
        } catch (error) {
          setFileError(error instanceof Error ? error.message : 'Could not read the selected files.');
        }
      }
    } finally {
      comparingFiles.current = false;
      setCheckingFiles(false);
    }
  };

  const submit = () => {
    if (deferredSend.current || comparingFiles.current) return;
    const text = value.trim() || (files.length ? 'Analyze the attached files.' : '');
    if (!text) return;
    if (isCommandLine(text)) {
      const matches = suggestionsFor(text, catalog);
      if (text === '/' || (matches.length && !matches.some(row => row.key === text.slice(1).toLowerCase()))) {
        setCommandHint('Complete the command before sending.');
        return;
      }
    }
    if (text === inserting) return;
    if (checkingSelection && !canOpenDuringSelection(text)) {
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
        if (!handedOff) release(); // 绑定失败或被后续请求取代时，保留当前草稿。
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
    setIndex(-1);
  };

  const insert = async () => {
    if (checkingSelection || comparingFiles.current) return;
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
    // 中日文输入法组词期间不能触发快捷键。
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
        applyCompletion(active ?? suggestions[0]);
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
        if (value.trim().toLowerCase() === active.insert.trim().toLowerCase()) submit();
        else applyCompletion(active);
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
    setValue(`/${name}`);
    setCommandHint('');
    setDismissed(false);
    setIndex(-1);
    ref.current?.focus();
  };

  const hasDraft = Boolean(value.trim() || files.length);
  const commandDraft = isCommandLine(value);

  return (
    <div className="composer" onPaste={event => {
      const selection = clipboardFiles(event.clipboardData);
      if (selection.length) {
        event.preventDefault();
        void addFiles(selection);
      }
    }}>
      {busy && (
        <div className="progress" data-testid="progress">
          <span>{progress || 'Working...'}</span>
        </div>
      )}
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
      {files.length > 0 && (
        <ul className="attachment-list" aria-label="Attachments">
          {files.map((file, index) => {
            const extension = file.name.match(/^.+(\.[a-z0-9]{1,10})$/i)?.[1] ?? '';
            const stem = file.name.slice(0, file.name.length - extension.length);
            return (
              <li className="attachment-preview" key={index}>
                <DraftAttachmentIcon file={file} />
                <span className="attachment-name" title={file.name}>
                  <bdi className="attachment-stem">{stem}</bdi>{extension}
                </span>
                <button type="button" className="attachment-remove" disabled={waitingToSend || checkingFiles}
                  aria-label={`Remove ${file.name}`}
                  onClick={() => setFiles(current => current.filter((_, position) => position !== index))}>
                  <span aria-hidden="true">×</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {fileError && <div className="progress" role="alert"><span>{fileError}</span></div>}
      <div className="composer-lane">
        <SurfacePresence>
          {showSuggestions && suggestions.length > 0 && <CommandSuggestions
            suggestions={suggestions} index={index} onPick={applyCompletion} />}
        </SurfacePresence>
        <div className="composer-card">
          <input ref={picker} type="file" multiple hidden disabled={!attachmentPolicy || waitingToSend || checkingFiles} aria-label="Select attachments"
            accept={attachmentPolicy?.extensions.join(',')}
            onChange={event => {
              const selection = Array.from(event.target.files ?? []);
              event.target.value = '';
              void addFiles(selection);
            }} />
          <textarea
            ref={ref}
            value={value}
            readOnly={waitingToSend}
            rows={2}
            aria-label="Message"
            data-testid="composer-input"
            placeholder="Message Syvrio"
            onChange={(event) => {
              setValue(event.target.value);
              setCommandHint('');
              setDismissed(false);
              setIndex(-1);
            }}
            onKeyDown={onKeyDown}
          />
          {commandHint && <div className="confirm-message" role="status">{commandHint}</div>}
          <div className="composer-bar">
            <button type="button" className="icon-button composer-attach"
              disabled={!attachmentPolicy || waitingToSend || checkingFiles} aria-label="Attach files" title="Attach files" onClick={openFiles ?? (() => picker.current?.click())}>
              <PlusIcon size={18} />
            </button>
            <div className="composer-actions">
              {busy && commandDraft && (
                <button type="button" className="composer-secondary" disabled={checkingFiles || (checkingSelection && !canOpenDuringSelection(value))} onClick={submit}>Run</button>
              )}
              {busy && hasDraft && !commandDraft && (
                <>
                  {onInsert && !files.length && (
                    <button type="button" className="composer-secondary" onClick={() => void insert()}
                      title="Insert at the next safe boundary (Ctrl+Enter)"
                      disabled={checkingFiles || checkingSelection || inserting !== null}>{inserting ? 'Waiting…' : 'Insert'}</button>
                  )}
                  <button type="button" className="composer-secondary" onClick={submit}
                    disabled={checkingFiles || checkingSelection || inserting === value.trim()} aria-label="Queue message"
                    title="Send after the current reply (Enter)">Queue</button>
                </>
              )}
              <ContextMeter reading={meterReading} usage={contextUsage} newChat={newChat} compaction={compaction} />
              <button type="button" className={busy ? 'stop' : 'send'} onClick={busy ? onStop : submit}
                disabled={waitingToSend || (!busy && checkingFiles) || (checkingSelection && !canOpenDuringSelection(value) && (!onSendAfterSelection || busy || commandDraft)) || (!busy && !hasDraft)}
                aria-label={busy ? 'Stop' : 'Send'} title={busy ? 'Stop generating' : 'Send'}>
                {busy ? <StopIcon size={18} /> : <SendIcon />}
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
