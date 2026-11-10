import { useEffect, useRef } from 'react';
import type { Segment } from '../state/chat';

type ThinkingSegment = Extract<Segment, { kind: 'thinking' }>;

/**
 * Live thinking shows only the newest three lines. Once the answer starts the
 * pane collapses to one summary row; expanding uses a fixed-height scroll area
 * so a long reasoning trace never stretches the chat layout.
 */
export function Thinking({
  segment,
  onToggle,
}: {
  segment: ThinkingSegment;
  onToggle: () => void;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const preview = !segment.done && !segment.collapsed && !segment.expanded;
  const showBody = !segment.collapsed || segment.expanded;
  const body = preview
    ? segment.text.split('\n').slice(-3).join('\n')
    : segment.text;

  useEffect(() => {
    if (preview && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [segment.text, preview]);

  const label = segment.done
    ? segment.seconds != null
      ? `Thought for ${segment.seconds.toFixed(1)}s`
      : 'Thinking'
    : 'Thinking...';

  return (
    <div
      className={`thinking${preview ? ' thinking-live' : ''}${segment.expanded ? ' thinking-expanded' : ''}`}
      data-testid="thinking"
    >
      <button
        type="button"
        className="thinking-toggle"
        onClick={onToggle}
        aria-expanded={showBody}
      >
        <span>{label}</span>
        <span className="thinking-hint">{segment.expanded ? 'Collapse' : 'Expand'}</span>
      </button>
      {showBody && (
        <div className="thinking-body" ref={bodyRef} data-testid="thinking-body">
          {body}
        </div>
      )}
    </div>
  );
}
