import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { ImagePreview, isPreviewImage } from './AttachmentImage';
import { attachmentUrl } from '../api/client';
import type { ChatMessage, Segment } from '../state/chat';
import { Collapsible } from './Collapsible';
import { Markdown } from './Markdown';
import { MessageActions, type MessageActionHandler, type OpenEditor } from './MessageActions';
import { ArrowUpIcon } from './Icons';
import { SurfacePresence } from './SurfacePresence';
import { Thinking } from './Thinking';

type ToolSegment = Extract<Segment, { kind: 'tool' }>;
type InfoMessage = Extract<ChatMessage, { role: 'info' }>;

function toolState(segment: ToolSegment): string {
  if (!segment.done) return 'Running';
  return segment.ok === false ? 'Failed' : 'Done';
}

/** A single tool call: a folded row whose result appears only on demand. */
function ToolView({ segment, onToggle }: { segment: ToolSegment; onToggle: () => void }) {
  const state = toolState(segment);
  if (!segment.display) {
    return (
      <div className="fold fold-static" data-testid="tool-event">
        <span className="fold-row">
          <span className="fold-label">{segment.name}</span>
          <span className="fold-meta">{state}</span>
        </span>
      </div>
    );
  }
  return (
    <Collapsible
      label={segment.name}
      meta={state}
      expanded={segment.expanded}
      onToggle={onToggle}
      testId="tool-event"
      bodyTestId="tool-body"
    >
      {segment.display}
    </Collapsible>
  );
}

/** Several calls in a row read as one line, expanded into a list. */
function ToolGroup({
  segments,
  onToggle,
}: {
  segments: ToolSegment[];
  onToggle: (segmentId: string) => void;
}) {
  if (segments.length === 1) {
    return <ToolView segment={segments[0]} onToggle={() => onToggle(segments[0].id)} />;
  }
  // The group rides on its first member's flag; expanding toggles every member
  // so a reloaded group never comes back half-open.
  const expanded = segments[0].expanded;
  const running = segments.some((segment) => !segment.done);
  return (
    <Collapsible
      label={`${segments.length} tool calls`}
      meta={running ? 'Running' : 'Done'}
      expanded={expanded}
      onToggle={() => segments.forEach((segment) => onToggle(segment.id))}
      testId="tool-group"
      bodyTestId="tool-group-body"
    >
      {segments.map((segment) => (
        <div className="tool-item" key={segment.id}>
          <div className="tool-item-head">
            <span className="tool-item-name">{segment.name}</span>
            <span className="tool-item-state">{toolState(segment)}</span>
          </div>
          {segment.display && <div className="tool-item-body">{segment.display}</div>}
        </div>
      ))}
    </Collapsible>
  );
}

/** Compacted history stays folded; command output keeps its normal row. */
function InfoLine({ message }: { message: InfoMessage }) {
  const [open, setOpen] = useState(false);
  if (!message.folded) return <div className="info-line">{message.content}</div>;
  return (
    <Collapsible
      label={message.title || 'Summary'}
      expanded={open}
      onToggle={() => setOpen((value) => !value)}
      className="info-fold"
      testId="info-summary"
      bodyTestId="info-summary-body"
    >
      {message.content}
    </Collapsible>
  );
}

function SegmentView({ segment, onToggle }: { segment: Segment; onToggle: () => void }) {
  if (segment.kind === 'thinking') return <Thinking segment={segment} onToggle={onToggle} />;
  if (segment.kind === 'text') return <Markdown text={segment.content} />;
  if (segment.kind === 'tool') return <ToolView segment={segment} onToggle={onToggle} />;
  return <div className="event-line">{segment.text}</div>;
}

/** Consecutive tool segments collapse into a single folded group. */
function renderSegments(segments: Segment[], onToggle: (id: string) => void): ReactNode[] {
  const nodes: ReactNode[] = [];
  let group: ToolSegment[] = [];
  const flush = () => {
    if (!group.length) return;
    nodes.push(<ToolGroup key={`group-${group[0].id}`} segments={group} onToggle={onToggle} />);
    group = [];
  };
  for (const segment of segments) {
    if (segment.kind === 'tool') {
      group.push(segment);
      continue;
    }
    flush();
    nodes.push(
      <SegmentView
        key={segment.id}
        segment={segment}
        onToggle={() => onToggle(segment.id)}
      />,
    );
  }
  flush();
  return nodes;
}

function HistoryLoading() {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setVisible(true), 200);
    return () => window.clearTimeout(timer);
  }, []);
  return visible ? <p className="history-loading" role="status">Loading conversation…</p> : null;
}

export function MessageList({
  messages,
  sessionId,
  selectionVersion,
  sendRevision = 0,
  onToggle,
  onMessageAction,
  onEdit,
  undoing = false,
  archived = false,
  loading = false,
}: {
  messages: ChatMessage[];
  sessionId: string | null;
  selectionVersion?: number;
  sendRevision?: number;
  onToggle: (segmentId: string) => void;
  onMessageAction?: MessageActionHandler;
  onEdit?: OpenEditor;
  undoing?: boolean;
  archived?: boolean;
  loading?: boolean;
}) {
  const latestUser = [...messages].reverse().find((message) => message.role === 'user');
  const ref = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const returnFrame = useRef<number | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const lastTop = useRef(0);
  const previousSession = useRef<string | null | undefined>(undefined);
  // A first receipt assigns a session ID without a navigation. Only a new selection enters.
  const lastShownSelection = useRef(selectionVersion ?? 0);
  const contentReady = !loading || messages.length > 0;

  useLayoutEffect(() => {
    if (!contentReady || selectionVersion === undefined || selectionVersion <= lastShownSelection.current) return;
    lastShownSelection.current = selectionVersion;
    const content = contentRef.current;
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    if (!content?.animate || !reducedMotion || reducedMotion.matches) return;
    const entrance = content.animate([
      { opacity: 0.8, transform: 'translateY(-3px)' },
      { opacity: 1, transform: 'none' },
    ], { duration: 150, easing: 'ease-out' });
    const stop = () => entrance.cancel();
    reducedMotion.addEventListener?.('change', stop);
    return () => {
      reducedMotion.removeEventListener?.('change', stop);
      stop();
    };
  }, [contentReady, selectionVersion]);

  const updateFollow = useCallback((element: HTMLDivElement) => {
    if (returnFrame.current !== null) return;
    const top = element.scrollTop;
    const distance = element.scrollHeight - top - element.clientHeight;
    const nearBottom = distance < 48;
    // An event can arrive after content grows, while the scroll offset still
    // equals the previous bottom. Only upward movement pauses following.
    if (nearBottom || top < lastTop.current) following.current = nearBottom;
    lastTop.current = top;
    setShowJump(!following.current && element.clientHeight > 0 && distance > element.clientHeight / 2);
  }, []);

  const followLatest = useCallback((element: HTMLDivElement, sessionChanged = false) => {
    if (sessionChanged) {
      if (returnFrame.current !== null) cancelAnimationFrame(returnFrame.current);
      returnFrame.current = null;
      following.current = true;
      setShowJump(false);
    } else {
      if (returnFrame.current !== null) return;
      updateFollow(element);
    }
    const bottom = element.scrollHeight - element.clientHeight;
    if (following.current && (sessionChanged || element.scrollTop < bottom)) {
      // Smooth scrolling emits intermediate events and can fall behind streamed updates.
      element.scrollTo({ top: element.scrollHeight, behavior: 'instant' });
      lastTop.current = element.scrollTop;
    }
  }, [updateFollow]);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const sessionChanged = previousSession.current !== sessionId;
    previousSession.current = sessionId;
    followLatest(element, sessionChanged);
  }, [followLatest, messages, sessionId]);

  const jumpToBottom = useCallback((button?: HTMLButtonElement) => {
    const list = ref.current;
    if (!list) return;
    if (button) {
      button.dataset.activated = 'true';
      list.focus({ preventScroll: true });
    }
    if (returnFrame.current !== null) cancelAnimationFrame(returnFrame.current);
    const distance = list.scrollHeight - list.clientHeight - list.scrollTop;
    const motion = window.matchMedia?.('(prefers-reduced-motion: no-preference)');
    if (distance <= 1 || !motion?.matches) {
      followLatest(list, true);
      setAnnouncement('At latest message');
      return;
    }

    following.current = false;
    setShowJump(false);
    setAnnouncement('Scrolling to latest message');
    const start = performance.now();
    const duration = Math.min(540, 260 + 70 * Math.log1p(distance / Math.max(1, list.clientHeight)));
    let previous = 0;
    const advance = (now: number) => {
      const t = Math.min(1, Math.max(0, (now - start) / duration));
      if (t >= 1 || !motion.matches) {
        returnFrame.current = null;
        followLatest(list, true);
        setAnnouncement('At latest message');
        return;
      }
      // Apply the eased fraction of the remaining distance to the live bottom.
      const eased = t * t * (3 - 2 * t);
      const remaining = list.scrollHeight - list.clientHeight - list.scrollTop;
      list.scrollTo({ top: list.scrollTop + remaining * (eased - previous) / (1 - previous), behavior: 'instant' });
      previous = eased;
      returnFrame.current = requestAnimationFrame(advance);
    };
    returnFrame.current = requestAnimationFrame(advance);
  }, [followLatest]);

  const lastSend = useRef(sendRevision);
  useLayoutEffect(() => {
    if (lastSend.current === sendRevision) return;
    lastSend.current = sendRevision;
    jumpToBottom();
  }, [sendRevision, jumpToBottom]);

  useLayoutEffect(() => {
    const list = ref.current;
    if (!list) return;
    const interrupt = () => {
      if (returnFrame.current === null) return;
      cancelAnimationFrame(returnFrame.current);
      returnFrame.current = null;
      updateFollow(list);
      setAnnouncement('');
    };
    list.addEventListener('wheel', interrupt, { passive: true });
    list.addEventListener('pointerdown', interrupt);
    list.addEventListener('keydown', interrupt);
    return () => {
      if (returnFrame.current !== null) cancelAnimationFrame(returnFrame.current);
      returnFrame.current = null;
      list.removeEventListener('wheel', interrupt);
      list.removeEventListener('pointerdown', interrupt);
      list.removeEventListener('keydown', interrupt);
    };
  }, [followLatest, updateFollow]);

  useLayoutEffect(() => {
    const element = ref.current;
    const content = contentRef.current;
    if (!element || !content || typeof ResizeObserver === 'undefined') return;
    // Images can finish loading and the composer can resize after a message renders.
    const observer = new ResizeObserver(() => followLatest(element));
    observer.observe(element);
    observer.observe(content);
    return () => observer.disconnect();
  }, [followLatest]);

  return (
    <>
      <div
        className="messages"
        ref={ref}
        tabIndex={-1}
        onScroll={(event) => updateFollow(event.currentTarget)}
        data-testid="message-list"
      >
        <div className="messages-content" ref={contentRef}>
          {!contentReady && <HistoryLoading key={selectionVersion ?? sessionId} />}
          {messages.length === 0 && !loading && (
            <h1 className="welcome">
              <picture>
                <source media="(prefers-reduced-motion: reduce)" srcSet="/syvrio-wordmark.svg#still" />
                <img src="/syvrio-wordmark.svg" alt="Syvrio" width="224" height="66" />
              </picture>
            </h1>
          )}
          {messages.map((message) => {
            if (message.role === 'user') {
              return (
                <div className="turn user" key={message.id} data-testid="user-message">
                  <div className="bubble">{message.content}</div>
                  {sessionId && !!message.attachments?.length && (
                    <ul className="message-attachments" aria-label="Message attachments">
                      {message.attachments.map(file => {
                        const messageId = message.savedId || message.id;
                        return <li key={file.id}>
                          {isPreviewImage(file.media_type) && <ImagePreview name={file.name}
                            src={attachmentUrl(sessionId, messageId, file.id, archived, true)} />}
                          <a download={file.name} href={attachmentUrl(sessionId, messageId, file.id, archived)}>
                            {file.name}
                          </a>
                        </li>;
                      })}
                    </ul>
                  )}
                  {message === latestUser && message.savedId && sessionId && onMessageAction && (
                    <MessageActions key={sessionId + '/' + message.savedId} sessionId={sessionId}
                      messageId={message.savedId} undoing={undoing} onAction={onMessageAction} onEdit={onEdit} />
                  )}
                </div>
              );
            }
            if (message.role === 'info') {
              return <InfoLine key={message.id} message={message} />;
            }
            return (
              <div className="turn assistant" key={message.id} data-testid="assistant-message">
                {renderSegments(message.segments, onToggle)}
                {message.interrupted && <div className="stopped">Stopped</div>}
              </div>
            );
          })}
        </div>
      </div>
      <div className="jump-to-bottom">
        <SurfacePresence>
          {showJump && (
            <button type="button" aria-label="Scroll to bottom" title="Scroll to bottom"
              onClick={event => jumpToBottom(event.currentTarget)}>
              <ArrowUpIcon size={20} />
            </button>
          )}
        </SurfacePresence>
        <span className="sr-only" aria-live="polite">{announcement}</span>
      </div>
    </>
  );
}
