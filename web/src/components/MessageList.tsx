import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import type { ChatMessage, Segment } from '../state/chat';
import { Collapsible } from './Collapsible';
import { Markdown } from './Markdown';
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

export function MessageList({
  messages,
  sessionId,
  onToggle,
}: {
  messages: ChatMessage[];
  sessionId: string | null;
  onToggle: (segmentId: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const previousSession = useRef<string | null | undefined>(undefined);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const sessionChanged = previousSession.current !== sessionId;
    previousSession.current = sessionId;
    // A newly opened conversation always starts at its latest message, even if
    // the user had paused follow scrolling in the previous conversation.
    if (sessionChanged) following.current = true;
    if (!following.current) return;
    element.scrollTo({
      top: element.scrollHeight,
      // CSS scroll-behavior must not turn history restoration into a long
      // animated trip from the first message to the last one.
      behavior: sessionChanged ? 'instant' : 'smooth',
    });
  }, [messages, sessionId]);

  const handleScroll = () => {
    const element = ref.current;
    if (!element) return;
    // Scrolling up pauses follow; returning to the bottom resumes it.
    following.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48;
  };

  return (
    <div className="messages" ref={ref} onScroll={handleScroll} data-testid="message-list">
      {messages.length === 0 && (
        <div className="welcome">
          <span className="welcome-mark" aria-hidden="true">S</span>
          <h1>Syvrio</h1>
          <p>Private conversations with persistent local memory.</p>
        </div>
      )}
      {messages.map((message) => {
        if (message.role === 'user') {
          return (
            <div className="turn user" key={message.id} data-testid="user-message">
              <div className="bubble">{message.content}</div>
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
  );
}
