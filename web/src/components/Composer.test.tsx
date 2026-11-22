import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Composer } from './Composer';

function renderComposer() {
  return render(
    <Composer busy={false} progress="" onSend={() => {}} onStop={() => {}} />,
  );
}

it('replays the context ball rebound on activation and respects reduced motion', () => {
  renderComposer();
  const dial = screen.getByRole('button', { name: /Context estimate/ });
  const orb = dial.querySelector<HTMLElement>('.context-orb')!;
  const cancel = vi.fn();
  orb.animate = vi.fn().mockReturnValue({ cancel });
  const media = window.matchMedia('(prefers-reduced-motion: no-preference)');
  const preference = vi.spyOn(window, 'matchMedia').mockReturnValue({ ...media, matches: true });
  try {
    fireEvent.click(dial);
    expect(orb.animate).toHaveBeenCalledTimes(1);
    expect(dial.getAttribute('aria-expanded')).toBe('true');
    fireEvent.pointerDown(dial);
    expect(cancel).toHaveBeenCalled();
    fireEvent.click(dial);
    expect(orb.animate).toHaveBeenCalledTimes(2);
    preference.mockReturnValue({ ...media, matches: false });
    fireEvent.click(dial);
    expect(orb.animate).toHaveBeenCalledTimes(2);
    expect(dial.getAttribute('aria-expanded')).toBe('true');
  } finally {
    preference.mockRestore();
  }
});

it('uses the same primary button to send, stop, and send again', () => {
  const props = { progress: '', onSend: vi.fn(), onStop: vi.fn(), onInsert: vi.fn() };
  const { rerender } = render(<Composer {...props} busy={false} />);
  const primary = screen.getByRole('button', { name: 'Send' });
  rerender(<Composer {...props} busy />);
  expect(screen.getByRole('button', { name: 'Stop' })).toBe(primary);
  expect(screen.queryByRole('button', { name: 'Queue message' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Insert' })).toBeNull();

  const input = screen.getByRole('textbox', { name: 'Message' });
  fireEvent.change(input, { target: { value: 'Next question' } });
  expect(screen.getByRole('button', { name: 'Insert' })).toBeTruthy();
  fireEvent.click(primary);
  expect(props.onStop).toHaveBeenCalledOnce();
  expect(props.onSend).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Queue message' }));
  expect(props.onSend).toHaveBeenCalledWith('Next question');
  expect(screen.queryByRole('button', { name: 'Queue message' })).toBeNull();
  rerender(<Composer {...props} busy={false} />);
  expect(screen.getByRole('button', { name: 'Send' })).toBe(primary);
});

it('preserves unfinished input while the destination of a switch is unconfirmed', () => {
  const props = { busy: false, progress: '', onSend: vi.fn(), onStop: vi.fn() };
  const { rerender } = render(<Composer {...props} checkingSelection />);
  const input = screen.getByRole('textbox', { name: 'Message' });
  fireEvent.change(input, { target: { value: 'still drafting' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  expect(props.onSend).not.toHaveBeenCalled();
  expect((input as HTMLTextAreaElement).value).toBe('still drafting');
  rerender(<Composer {...props} />);
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(props.onSend).toHaveBeenCalledWith('still drafting');
});

it('uses the plus button to pick and send multiple attachments', () => {
  const onSend = vi.fn();
  render(<Composer busy={false} progress="" onSend={onSend} onStop={() => {}} />);
  const picker = screen.getByLabelText('Select attachments') as HTMLInputElement;
  const openPicker = vi.spyOn(picker, 'click');
  fireEvent.click(screen.getByRole('button', { name: 'Attach files' }));
  expect(openPicker).toHaveBeenCalledOnce();
  expect(picker.multiple).toBe(true);
  const files = [new File(['a'], 'one.txt'), new File(['b'], 'two.md')];
  const extra = new File(['c'], 'meeting-notes-with-a-long-name.pdf');
  fireEvent.change(picker, { target: { files: [...files, extra] } });
  fireEvent.click(screen.getByTitle(extra.name));
  expect(screen.getAllByRole('listitem')).toHaveLength(3);
  fireEvent.click(screen.getByRole('button', { name: `Remove ${extra.name}` }));
  expect(screen.getAllByRole('listitem')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  expect(onSend).toHaveBeenCalledWith('Analyze the attached files.', files);
});

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
