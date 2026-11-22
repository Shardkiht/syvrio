import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '../state/chat';
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
    expect(screen.getByRole('status').textContent).toContain('Loading');
    rerender(select(3, 'latest', [], true));
    expect(animate).toHaveBeenCalledTimes(1);
    rerender(select(3, 'latest', messages));
    expect(animate).toHaveBeenCalledTimes(2);
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

    // A late event from the previous scroll sees the new height at the old position.
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
    // The update can render before the browser dispatches the user's scroll event.
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

      // A larger composer reduces the viewport without changing message data.
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
