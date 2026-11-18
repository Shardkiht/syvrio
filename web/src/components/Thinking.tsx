import { useEffect, useRef } from 'react';
import type { Segment } from '../state/chat';
import { Collapsible } from './Collapsible';

type ThinkingSegment = Extract<Segment, { kind: 'thinking' }>;

/** Distance from the bottom that still counts as "watching the tail". */
const FOLLOW_THRESHOLD = 24;

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
  // The scroll container is the capped fold body, not the text node inside it.
  const bodyRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);

  useEffect(() => {
    const body = bodyRef.current;
    if (!segment.expanded || !body) return;
    // A finished trace is read from the top; a live one opens at the newest line.
    following.current = !segment.done;
    body.scrollTop = segment.done ? 0 : body.scrollHeight;
  }, [segment.expanded]);

  // Reasoning arrives as deltas: keep the tail visible unless the user scrolled up.
  useEffect(() => {
    const body = bodyRef.current;
    if (!segment.expanded || !body || !following.current) return;
    body.scrollTop = body.scrollHeight;
  }, [segment.text, segment.expanded]);

  useEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    const onScroll = () => {
      following.current =
        body.scrollHeight - body.scrollTop - body.clientHeight <= FOLLOW_THRESHOLD;
    };
    body.addEventListener('scroll', onScroll, { passive: true });
    return () => body.removeEventListener('scroll', onScroll);
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
      bodyRef={bodyRef}
    >
      <div className="thinking-text">{segment.text}</div>
    </Collapsible>
  );
}
