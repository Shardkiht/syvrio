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
