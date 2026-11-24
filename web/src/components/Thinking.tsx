import { useEffect, useRef } from 'react';
import type { Segment } from '../state/chat';
import { Collapsible } from './Collapsible';

type ThinkingSegment = Extract<Segment, { kind: 'thinking' }>;

/** 距底部多近时仍视为正在跟随最新内容。 */
const FOLLOW_THRESHOLD = 24;

/**
 * 思考流式输出时也默认折叠，只在摘要行显示进行中或耗时提示。
 * 用户主动展开后才能阅读思考记录。长记录使用限高的滚动区域，避免撑高聊天布局。
 */
export function Thinking({
  segment,
  onToggle,
}: {
  segment: ThinkingSegment;
  onToggle: () => void;
}) {
  // 设有高度上限的折叠正文容器负责滚动，内部文本节点只显示内容。
  const bodyRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);

  useEffect(() => {
    const body = bodyRef.current;
    if (!segment.expanded || !body) return;
    // 已完成的思考从头阅读，正在生成的思考打开时定位到最新一行。
    following.current = !segment.done;
    body.scrollTop = segment.done ? 0 : body.scrollHeight;
  }, [segment.expanded]);

  // 思考内容按增量到达，用户未向上滚动时让末尾保持可见。
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
