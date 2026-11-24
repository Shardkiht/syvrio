import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { initialChatState, loadHistory, toggleSegment } from './chat';
import type { ChatMessage } from './chat';
import { MessageList } from './MessageList';

const messages: ChatMessage[] = [
  { id: 'u1', role: 'user', content: 'question' },
  {
    id: 'a1', role: 'assistant', streaming: false, interrupted: false,
    segments: [{ kind: 'text', id: 'c1', content: 'latest answer' }],
  },
];

function measureList() {
  const list = screen.getByTestId('message-list');
  Object.defineProperty(list, 'scrollHeight', { configurable: true, value: 2400 });
  Object.defineProperty(list, 'clientHeight', { configurable: true, value: 400 });
  const scrollTo = vi.spyOn(list, 'scrollTo');
  return { list, scrollTo };
}

it.each([false, true])('previews stored images and keeps their download links (archived=%s)', archived => {
  const attachments = [
    { id: 'i', name: 'photo.jpg', size: 42, media_type: 'image/jpeg' },
    { id: 'p', name: 'notes.pdf', size: 3, media_type: 'application/pdf' },
  ];
  render(<MessageList messages={[{ id: 'local', savedId: 'saved', role: 'user', content: 'Look', attachments }]}
    sessionId="session" archived={archived} onToggle={() => {}} />);
  const thumb = screen.getByRole('button', { name: 'Preview photo.jpg' });
  const file = screen.getByRole('link', { name: 'photo.jpg' });
  const url = '/api/sessions/session/messages/saved/attachments/i';
  expect(file.getAttribute('href')).toBe(url + (archived ? '?archived=true' : ''));
  const src = url + (archived ? '?archived=true&' : '?') + 'preview=true';
  expect(thumb.querySelector('img')?.getAttribute('src')).toBe(src);
  expect(screen.queryByRole('button', { name: 'Preview notes.pdf' })).toBeNull();
  fireEvent.click(thumb);
  expect(screen.getByRole('img', { name: 'photo.jpg' }).getAttribute('src')).toBe(src);
  fireEvent.click(screen.getByRole('button', { name: 'Close image preview' }));
  fireEvent.error(thumb.querySelector('img')!);
  expect(screen.getByText('Preview unavailable')).toBeTruthy();
  expect(screen.getByRole('link', { name: 'photo.jpg' })).toBe(file);
});

it.each([false, true])('loads the full persisted tool result only when expanded (archived=%s)', async archived => {
  const read = vi.spyOn(api, 'toolResult').mockRejectedValueOnce(new Error('Try again.'))
    .mockResolvedValue({ content: 'Full persisted result and tail' });
  const history = loadHistory(initialChatState, {
    session_id: 's1', title: '', current: true, summary: null, has_earlier: false,
    items: [{ id: 'record-1', at: '', role: 'tool', tool_name: 'read_file', tool_calls: [],
      tool_ok: true, content: 'Preview', tool_content_truncated: true, tool_content_length: 900,
      reasoning: null, interrupted: false }],
  }, archived);
  let state = history;
  function view() {
    return <MessageList messages={state.messages} sessionId="s1" onToggle={id => {
      state = toggleSegment(state, id);
      rerender(view());
    }} />;
  }
  const { rerender } = render(view());
  expect(read).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: /read_file/ }));
  expect(await screen.findByRole('alert')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(await screen.findByText('Full persisted result and tail')).toBeTruthy();
  expect(read).toHaveBeenCalledWith('s1', 'record-1', archived, expect.any(AbortSignal));
  read.mockRestore();
});

describe('conversation entry motion', () => {
  function select(version: number, session: string | null, entries: ChatMessage[], loading = false) {
    return <MessageList selectionVersion={version} sessionId={session} messages={entries}
      loading={loading} onToggle={() => {}} />;
  }

  it('offers a still water surface on the home view when reduced motion is requested', () => {
    render(select(0, null, []));
    const heading = screen.getByRole('heading', { name: 'Syvrio', level: 1 });
    const still = heading.querySelector('source[media="(prefers-reduced-motion: reduce)"]');
    expect(still?.getAttribute('srcset')).toBe('/syvrio-wordmark.svg#still');
  });

  function recordEntrances() {
    const content = screen.getByTestId('message-list').firstElementChild!;
    const animate = vi.fn(() => ({ cancel: vi.fn() }));
    Object.defineProperty(content, 'animate', { configurable: true, value: animate });
    return animate;
  }

  it('shows cached content immediately and cancels its entrance when a newer selection starts', () => {
    const { rerender } = render(select(0, 'a', messages));
    const animate = recordEntrances();
    rerender(select(1, 'cached', messages));
    expect(animate).toHaveBeenCalledOnce();
    const entrance = animate.mock.results[0].value;
    rerender(select(1, 'cached', [...messages]));
    expect(entrance.cancel).not.toHaveBeenCalled();

    rerender(select(2, 'cold', [], true));
    expect(entrance.cancel).toHaveBeenCalledOnce();
    expect(screen.queryByText('Loading conversation…')).toBeNull();
    rerender(select(3, 'latest', [], true));
    expect(animate).toHaveBeenCalledTimes(1);
    rerender(select(3, 'latest', messages));
    expect(animate).toHaveBeenCalledTimes(2);
  });

  it('skips the hint on fast reads and times it from the latest selection on slow reads', () => {
    vi.useFakeTimers();
    try {
      const hint = () => screen.queryByText('Loading conversation…');
      const tick = (ms: number) => act(() => { vi.advanceTimersByTime(ms); });
      const { rerender } = render(select(1, 'a', [], true));
      tick(40);
      expect(hint()).toBeNull();
      rerender(select(1, 'a', messages));
      expect(screen.getByText('latest answer')).toBeTruthy();
      tick(200);
      expect(hint()).toBeNull();

      rerender(select(2, 'b', [], true));
      tick(150);
      rerender(select(3, 'c', [], true));
      tick(199);
      expect(hint()).toBeNull();
      tick(1);
      expect(screen.getByRole('status').textContent).toBe('Loading conversation…');
      rerender(select(3, 'c', messages));
      expect(hint()).toBeNull();
      expect(screen.getByText('latest answer')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('animates the new conversation once and keeps its first receipt and streaming messages steady', () => {
    const { rerender } = render(select(0, 'a', messages));
    const animate = recordEntrances();
    rerender(select(1, null, []));
    expect(animate).toHaveBeenCalledOnce();
    rerender(select(1, 'new-id', messages.slice(0, 1)));
    rerender(select(1, 'new-id', messages));
    expect(animate).toHaveBeenCalledOnce();
    expect(animate.mock.results[0].value.cancel).not.toHaveBeenCalled();
  });

  it('displays the selected transcript immediately with reduced motion', () => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const preference = vi.spyOn(window, 'matchMedia').mockReturnValue({ ...media, matches: true });
    try {
      const { rerender } = render(select(0, 'a', []));
      const animate = recordEntrances();
      rerender(select(1, 'b', messages));
      expect(screen.getByText('latest answer')).toBeTruthy();
      expect(animate).not.toHaveBeenCalled();
    } finally {
      preference.mockRestore();
    }
  });
});

describe('message list scrolling', () => {
  it('returns on send with smooth motion, preserves focus and follows the new bottom', () => {
    const view = (revision: number) => <><textarea aria-label="Draft" /><MessageList messages={messages}
      sessionId="s" sendRevision={revision} onToggle={() => {}} /></>;
    const { rerender } = render(view(0));
    const { list, scrollTo } = measureList();
    list.scrollTop = 2000;
    fireEvent.scroll(list);
    list.scrollTop = 800;
    fireEvent.scroll(list);
    const input = screen.getByRole('textbox');
    input.focus();
    const media = window.matchMedia('(prefers-reduced-motion: no-preference)');
    const motion = vi.spyOn(window, 'matchMedia').mockReturnValue({ ...media, matches: true });
    let frame: FrameRequestCallback;
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => { frame = callback; return 1; });
    const clock = vi.spyOn(performance, 'now').mockReturnValue(0);
    scrollTo.mockImplementation(options => { list.scrollTop = (options as ScrollToOptions).top!; });
    try {
      rerender(view(1));
      act(() => frame(80));
      expect(list.scrollTop).toBeGreaterThan(800);
      expect(list.scrollTop).toBeLessThan(2000);
      expect(document.activeElement).toBe(input);
      Object.defineProperty(list, 'scrollHeight', { value: 3000 });
      act(() => frame(180));
      expect(list.scrollTop).toBeLessThan(2600);
      act(() => frame(550));
      expect(scrollTo).toHaveBeenLastCalledWith({ top: 3000, behavior: 'instant' });
      expect(screen.getByText('At latest message')).toBeTruthy();
    } finally { raf.mockRestore(); clock.mockRestore(); motion.mockRestore(); }
  });

  it('offers a jump only beyond half a viewport and resumes following on click', () => {
    const { rerender } = render(<MessageList messages={messages} sessionId="s" onToggle={() => {}} />);
    const { list, scrollTo } = measureList();
    const button = () => screen.queryByRole('button', { name: 'Scroll to bottom' });
    list.scrollTop = 2000;
    fireEvent.scroll(list);
    list.scrollTop = 1800;
    fireEvent.scroll(list);
    expect(button()).toBeNull();
    list.scrollTop = 1799;
    fireEvent.scroll(list);
    fireEvent.click(button()!);
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 2400, behavior: 'instant' });
    expect(button()).toBeNull();
    Object.defineProperty(list, 'scrollHeight', { value: 3000 });
    rerender(<MessageList messages={[...messages]} sessionId="s" onToggle={() => {}} />);
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 3000, behavior: 'instant' });
  });

  it('jumps straight to the latest message when the session changes', () => {
    const { rerender } = render(
      <MessageList messages={[]} sessionId="s-old" onToggle={() => {}} />,
    );
    const { scrollTo } = measureList();

    rerender(<MessageList messages={messages} sessionId="s-new" onToggle={() => {}} />);

    expect(screen.getByTestId('message-list').textContent).toContain('latest answer');
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 2400, behavior: 'instant' });
  });

  it('keeps following when a scroll event arrives during transcript growth', () => {
    const { rerender } = render(
      <MessageList messages={messages} sessionId="s-1" onToggle={() => {}} />,
    );
    const { list, scrollTo } = measureList();
    list.scrollTop = 2000;
    fireEvent.scroll(list);

    // 前一次滚动的延迟事件可能在高度更新后仍报告旧位置。
    Object.defineProperty(list, 'scrollHeight', { value: 3000 });
    fireEvent.scroll(list);
    scrollTo.mockClear();

    rerender(
      <MessageList messages={[...messages]} sessionId="s-1" onToggle={() => {}} />,
    );

    expect(scrollTo).toHaveBeenLastCalledWith({ top: 3000, behavior: 'instant' });
  });

  it('pauses when the reader scrolls up and resumes after returning to the bottom', () => {
    const { rerender } = render(
      <MessageList messages={messages} sessionId="s-1" onToggle={() => {}} />,
    );
    const { list, scrollTo } = measureList();
    list.scrollTop = 2000;
    fireEvent.scroll(list);
    // 浏览器派发用户滚动事件前，页面可能已经渲染了更新。
    list.scrollTop = 1200;
    scrollTo.mockClear();
    rerender(<MessageList messages={[...messages]} sessionId="s-1" onToggle={() => {}} />);
    expect(scrollTo).not.toHaveBeenCalled();

    list.scrollTop = 2000;
    fireEvent.scroll(list);
    Object.defineProperty(list, 'scrollHeight', { value: 3000 });
    rerender(<MessageList messages={[...messages]} sessionId="s-1" onToggle={() => {}} />);
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 3000, behavior: 'instant' });
  });

  it('stays at the end on layout changes and returns to follow after switching sessions', () => {
    const observe = vi.fn();
    const disconnect = vi.fn();
    let resize = () => {};
    vi.stubGlobal('ResizeObserver', class {
      observe = observe;
      disconnect = disconnect;

      constructor(callback: () => void) {
        resize = callback;
      }
    });
    try {
      const { rerender, unmount } = render(
        <MessageList messages={messages} sessionId="s-1" onToggle={() => {}} />,
      );
      const { list, scrollTo } = measureList();
      expect(observe).toHaveBeenCalledWith(list);
      expect(observe).toHaveBeenCalledWith(list.firstElementChild);
      list.scrollTop = 2000;
      fireEvent.scroll(list);
      scrollTo.mockClear();

      // 输入框变大后，消息数据虽未变，可用视口已经缩小。
      Object.defineProperty(list, 'clientHeight', { value: 310 });
      resize();
      expect(scrollTo).toHaveBeenLastCalledWith({ top: 2400, behavior: 'instant' });

      list.scrollTop = 1000;
      fireEvent.scroll(list);
      scrollTo.mockClear();
      Object.defineProperty(list, 'scrollHeight', { value: 3000 });
      resize();
      expect(scrollTo).not.toHaveBeenCalled();

      rerender(<MessageList messages={messages} sessionId="s-2" onToggle={() => {}} />);
      expect(scrollTo).toHaveBeenLastCalledWith({ top: 3000, behavior: 'instant' });
      unmount();
      expect(disconnect).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
