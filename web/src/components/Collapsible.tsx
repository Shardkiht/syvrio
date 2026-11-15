import { useId, type ReactNode } from 'react';

/**
 * One row of folded chrome: a pill summary plus an optional body.
 * Everything that is not the answer itself (reasoning, tool calls, compaction
 * notes) starts collapsed so the transcript reads as conversation, not logs.
 */
export function Collapsible({
  label,
  meta,
  expanded,
  onToggle,
  children,
  className,
  testId,
  bodyTestId,
}: {
  label: ReactNode;
  meta?: ReactNode;
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
  className?: string;
  testId?: string;
  bodyTestId?: string;
}) {
  const root = ['fold', expanded ? 'fold-open' : '', className ?? ''].filter(Boolean).join(' ');
  const bodyId = useId();
  return (
    <div className={root} data-testid={testId}>
      <button
        type="button"
        className="fold-summary"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={expanded ? bodyId : undefined}
      >
        <span className="fold-label">{label}</span>
        {meta != null && <span className="fold-meta">{meta}</span>}
        <span className="fold-hint">{expanded ? 'Collapse' : 'Expand'}</span>
        <svg
          className="fold-chevron"
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
          focusable="false"
        >
          <path d="M9 5l7 7-7 7" />
        </svg>
      </button>
      {expanded && (
        <div className="fold-body" id={bodyId} data-testid={bodyTestId}>
          {children}
        </div>
      )}
    </div>
  );
}
