import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Composer } from './Composer';

function renderComposer() {
  return render(
    <Composer busy={false} progress="" onSend={() => {}} onStop={() => {}} />,
  );
}

/**
 * jsdom has no layout engine, so the tests pin the measurements the fit effect
 * reads. `offset`/`client` include the 1px borders + 11px padding of the real
 * textarea; `scroll` is the content height the browser would report.
 */
function measure(
  input: HTMLTextAreaElement,
  sizes: { scroll: number; offset: number; client: number },
) {
  Object.defineProperty(input, 'scrollHeight', { configurable: true, value: sizes.scroll });
  Object.defineProperty(input, 'offsetHeight', { configurable: true, value: sizes.offset });
  Object.defineProperty(input, 'clientHeight', { configurable: true, value: sizes.client });
}

describe('Composer sizing', () => {
  it('starts as a three-line box without a scrollbar', () => {
    renderComposer();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    expect(input.getAttribute('rows')).toBe('3');
    expect(input.style.overflowY).toBe('hidden');
    expect(input.style.height).toBe('');
  });

  it('grows with the content but keeps the scrollbar hidden below the cap', () => {
    renderComposer();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    measure(input, { scroll: 110, offset: 87, client: 85 });
    fireEvent.change(input, { target: { value: 'one\ntwo\nthree\nfour' } });
    expect(input.style.height).toBe('112px');
    expect(input.style.overflowY).toBe('hidden');
  });

  it('caps at the maximum height and only then scrolls', () => {
    renderComposer();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    measure(input, { scroll: 400, offset: 87, client: 85 });
    fireEvent.change(input, { target: { value: 'x\n'.repeat(20) } });
    expect(input.style.height).toBe('200px');
    expect(input.style.overflowY).toBe('auto');
  });

  it('returns to the three-line box after sending', () => {
    renderComposer();
    const input = screen.getByTestId('composer-input') as HTMLTextAreaElement;
    measure(input, { scroll: 110, offset: 87, client: 85 });
    fireEvent.change(input, { target: { value: 'one\ntwo\nthree\nfour' } });
    expect(input.style.height).toBe('112px');
    measure(input, { scroll: 85, offset: 87, client: 85 });
    fireEvent.change(input, { target: { value: '' } });
    expect(input.style.height).toBe('');
    expect(input.style.overflowY).toBe('hidden');
  });
});
