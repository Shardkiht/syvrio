import { useEffect, useRef } from 'react';
import type { Segment } from '../state/chat';
import { Collapsible } from './Collapsible';

type ThinkingSegment = Extract<Segment, { kind: 'thinking' }>;

/**
 * Reasoning is never shown inline, not even while it streams: the summary row
 * carries the only signal ("Thinking…" → "Thought for 3.2s") and expanding is
 * an explicit opt-in. A long trace lives in a fixed-height scroll area so it
 * can never stretch the chat layout.
 */
export function Thinking({
  segment,
  onToggle,
}: {
  segment: ThinkingSegment;
  onToggle: () => void;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (segment.expanded && bodyRef.current) bodyRef.current.scrollTop = 0;
  }, [segment.expanded]);

  const label = segment.done
    ? segment.seconds != null
      ? `Thought for ${segment.seconds.toFixed(1)}s`
      : 'Thought'
    : 'Thinking...';

  return (
    <Collapsible
      label={label}
      meta={!segment.done ? <span className="fold-pulse" aria-hidden="true" /> : undefined}
      expanded={segment.expanded}
      onToggle={onToggle}
      className={`thinking${segment.expanded ? ' thinking-expanded' : ''}`}
      testId="thinking"
      bodyTestId="thinking-body"
    >
      <div className="thinking-text" ref={bodyRef}>{segment.text}</div>
    </Collapsible>
  );
}
