import { useId, type ReactNode, type Ref } from 'react';

/**
 * 可折叠内容占一行，由胶囊状摘要和可选正文组成。
 * 思考、工具调用及压缩提示默认折叠，让读者能直接阅读对话正文。
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
  bodyRef,
}: {
  label: ReactNode;
  meta?: ReactNode;
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
  className?: string;
  testId?: string;
  bodyTestId?: string;
  /** 返回设有高度上限的滚动区域，供调用方跟随流式内容。 */
  bodyRef?: Ref<HTMLDivElement>;
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
        <div className="fold-body" id={bodyId} data-testid={bodyTestId} ref={bodyRef}>
          {children}
        </div>
      )}
    </div>
  );
}
