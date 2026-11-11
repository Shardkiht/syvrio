import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '../state/chat';
import { MessageList } from './MessageList';

const messages: ChatMessage[] = [
  { id: 'u1', role: 'user', content: 'question' },
  {
    id: 'a1', role: 'assistant', streaming: false, interrupted: false,
    segments: [{ kind: 'text', id: 'c1', content: 'latest answer' }],
  },
];

describe('message list scrolling', () => {
  const scrollTo = vi.fn();

  beforeEach(() => {
    scrollTo.mockReset();
    Object.defineProperty(HTMLElement.prototype, 'scrollTo', {
      configurable: true,
      writable: true,
      value: scrollTo,
    });
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      configurable: true,
      get: () => 2400,
    });
  });

  it('jumps straight to the latest message when the session changes', () => {
    const { rerender } = render(
      <MessageList messages={[]} sessionId="s-old" onToggle={() => {}} />,
    );
    scrollTo.mockClear();

    rerender(<MessageList messages={messages} sessionId="s-new" onToggle={() => {}} />);

    expect(screen.getByTestId('message-list').textContent).toContain('latest answer');
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 2400, behavior: 'instant' });
  });

  it('keeps smooth following for updates within the same session', () => {
    const { rerender } = render(
      <MessageList messages={messages} sessionId="s-1" onToggle={() => {}} />,
    );
    scrollTo.mockClear();

    rerender(
      <MessageList
        messages={[...messages, { id: 'u2', role: 'user', content: 'next question' }]}
        sessionId="s-1"
        onToggle={() => {}}
      />,
    );

    expect(scrollTo).toHaveBeenLastCalledWith({ top: 2400, behavior: 'smooth' });
  });
});
