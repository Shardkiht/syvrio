import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { Segment } from './chat';
import { Thinking } from './Thinking';

type ThinkingSegment = Extract<Segment, { kind: 'thinking' }>;

// jsdom 对所有元素都返回零尺寸，用模拟的测量值驱动有高度上限的折叠区域。
// 全套测试共用一个单线程 jsdom。测试后须还原原型上的覆盖，
// 避免后续文件继承这些尺寸。
let metrics = { scrollHeight: 600, clientHeight: 220 };

beforeEach(() => {
  metrics = { scrollHeight: 600, clientHeight: 220 };
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
    configurable: true,
    get: () => metrics.scrollHeight,
  });
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get: () => metrics.clientHeight,
  });
});

afterEach(() => {
  Reflect.deleteProperty(HTMLElement.prototype, 'scrollHeight');
  Reflect.deleteProperty(HTMLElement.prototype, 'clientHeight');
});

function segment(overrides: Partial<ThinkingSegment> = {}): ThinkingSegment {
  return {
    kind: 'thinking',
    id: 't1',
    phase: 0,
    text: '',
    done: false,
    collapsed: true,
    expanded: false,
    seconds: null,
    ...overrides,
  };
}

const body = () => screen.getByTestId('thinking-body');

describe('thinking scroll', () => {
  it('opens a live trace at its tail instead of the top', () => {
    const { rerender } = render(<Thinking segment={segment()} onToggle={() => {}} />);
    rerender(<Thinking segment={segment({ expanded: true, text: 'line one' })} onToggle={() => {}} />);

    expect(body().scrollTop).toBe(600);
  });

  it('opens a finished trace at the top', () => {
    const { rerender } = render(<Thinking segment={segment()} onToggle={() => {}} />);
    rerender(
      <Thinking
        segment={segment({ expanded: true, done: true, seconds: 1.5, text: 'thought' })}
        onToggle={() => {}}
      />,
    );

    expect(body().scrollTop).toBe(0);
  });

  it('keeps the reading position when the open trace finishes', () => {
    const { rerender } = render(
      <Thinking segment={segment({ expanded: true, text: 'thought' })} onToggle={() => {}} />,
    );
    const element = body();
    element.scrollTop = 100;
    fireEvent.scroll(element);

    rerender(
      <Thinking
        segment={segment({ expanded: true, done: true, text: 'thought' })}
        onToggle={() => {}}
      />,
    );
    expect(element.scrollTop).toBe(100);
  });

  it('follows reasoning text that streams in while expanded', () => {
    const { rerender } = render(
      <Thinking segment={segment({ expanded: true, text: 'line one' })} onToggle={() => {}} />,
    );
    metrics.scrollHeight = 900;
    rerender(
      <Thinking
        segment={segment({ expanded: true, text: 'line one\nline two' })}
        onToggle={() => {}}
      />,
    );

    expect(body().scrollTop).toBe(900);
  });

  it('stops following after the reader scrolls up, and resumes at the bottom', () => {
    const { rerender } = render(
      <Thinking segment={segment({ expanded: true, text: 'line one' })} onToggle={() => {}} />,
    );
    const element = body();
    element.scrollTop = 100;
    fireEvent.scroll(element);

    metrics.scrollHeight = 900;
    rerender(
      <Thinking
        segment={segment({ expanded: true, text: 'line one\nline two' })}
        onToggle={() => {}}
      />,
    );
    expect(element.scrollTop).toBe(100);

    element.scrollTop = 680; // 在 220 像素视口中滚动到 900 像素思考记录的底部。
    fireEvent.scroll(element);
    metrics.scrollHeight = 1200;
    rerender(
      <Thinking
        segment={segment({ expanded: true, text: 'line one\nline two\nline three' })}
        onToggle={() => {}}
      />,
    );

    expect(element.scrollTop).toBe(1200);
  });
});
