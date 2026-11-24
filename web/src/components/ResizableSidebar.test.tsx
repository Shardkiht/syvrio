import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ResizableSidebar } from './ResizableSidebar';

const key = 'syvrio.sidebar.width';
const frame = (open = true) => <ResizableSidebar open={open}>Chats</ResizableSidebar>;
const divider = () => screen.getByRole('separator', { name: 'Chat sidebar width' });
function resize(width: number) {
  vi.stubGlobal('innerWidth', width);
  act(() => window.dispatchEvent(new Event('resize')));
}
afterEach(() => { vi.unstubAllGlobals(); localStorage.removeItem(key); });

it('remembers a keyboard adjustment and restores the preferred width after the window expands', () => {
  resize(1280);
  const view = render(frame());
  expect(divider().getAttribute('aria-valuenow')).toBe('300');
  fireEvent.keyDown(divider(), { key: 'ArrowRight' });
  expect(localStorage.getItem(key)).toBe('310');
  fireEvent.keyDown(divider(), { key: 'End' });
  expect(localStorage.getItem(key)).toBe('480');
  resize(800);
  expect(divider().getAttribute('aria-valuemax')).toBe('360');
  expect(divider().getAttribute('aria-valuenow')).toBe('360');
  resize(390);
  expect(screen.queryByRole('separator')).toBeNull();
  resize(1200);
  view.rerender(frame(false));
  expect(screen.queryByRole('separator')).toBeNull();
  view.unmount();
  render(frame());
  expect(divider().getAttribute('aria-valuenow')).toBe('480');
  fireEvent.keyDown(divider(), { key: 'Home' });
  fireEvent.keyDown(divider(), { key: 'ArrowLeft' });
  expect(localStorage.getItem(key)).toBe('240');
  fireEvent.doubleClick(divider());
  expect(localStorage.getItem(key)).toBe('300');
});
