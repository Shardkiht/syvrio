import { useEffect, useRef } from 'react';
import type { ChatMessage, Segment } from '../state/chat';
import { Markdown } from './Markdown';
import { Thinking } from './Thinking';

function SegmentView({ segment, onToggle }: { segment: Segment; onToggle: () => void }) {
  if (segment.kind === 'thinking') return <Thinking segment={segment} onToggle={onToggle} />;
  if (segment.kind === 'text') return <Markdown text={segment.content} />;
  if (segment.kind === 'tool') {
    return (
      <div className="tool" data-testid="tool-event">
        <span className="tool-name">{segment.name}</span>
        <span className="tool-state">{segment.done ? 'Done' : 'Running'}</span>
        {segment.display && <span className="tool-display">{segment.display}</span>}
      </div>
    );
  }
  return <div className="event-line">{segment.text}</div>;
}

export function MessageList({
  messages,
  onToggle,
}: {
  messages: ChatMessage[];
  onToggle: (segmentId: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const following = useRef(true);

  useEffect(() => {
    const element = ref.current;
    if (element && following.current) element.scrollTop = element.scrollHeight;
  }, [messages]);

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
          <h1>Syvrio</h1>
          <p>Private conversations with persistent local memory.</p>
        </div>
      )}
      {messages.map((message) => {
        if (message.role === 'user') {
          return (
            <div className="turn user" key={message.id} data-testid="user-message">
              <div className="who">You</div>
              <div className="bubble">{message.content}</div>
            </div>
          );
        }
        if (message.role === 'info') {
          return <div className="info-line" key={message.id}>{message.content}</div>;
        }
        return (
          <div className="turn assistant" key={message.id} data-testid="assistant-message">
            <div className="who">Syvrio</div>
            {message.segments.map((segment) => (
              <SegmentView
                key={segment.id}
                segment={segment}
                onToggle={() => onToggle(segment.id)}
              />
            ))}
            {message.interrupted && <div className="stopped">Stopped</div>}
          </div>
        );
      })}
    </div>
  );
}
