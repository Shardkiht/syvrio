import { act, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SurfacePresence } from './SurfacePresence';

afterEach(() => { vi.restoreAllMocks(); });

function dialogs(...titles: string[]) {
  return <SurfacePresence modal>{titles.map(title =>
    <div key={title} className="overlay"><section role="dialog" aria-modal="true" aria-label={title} /></div>
  )}</SurfacePresence>;
}

function enableMotion() {
  const media = window.matchMedia('(prefers-reduced-motion: no-preference)');
  vi.spyOn(window, 'matchMedia').mockReturnValue({ ...media, matches: true });
}

function transitionOn(element: Element) {
  let finish!: () => void;
  const finished = new Promise<void>(resolve => { finish = resolve; });
  Object.defineProperty(element, 'getAnimations', { value: () => [{ finished }] });
  return finish;
}

it('keeps the backdrop mounted across switches, exits and quick reopens', async () => {
  enableMotion();
  const { container, rerender } = render(dialogs());
  const layer = container.querySelector('.modal-layer')!;
  expect(layer.hasAttribute('data-open')).toBe(false);
  rerender(dialogs('Settings'));
  const panel = screen.getByRole('dialog');
  const overlay = panel.parentElement!;
  const slideDone = transitionOn(panel);
  rerender(dialogs('Pending'));
  expect(container.querySelector('.modal-layer')).toBe(layer);
  expect(layer.hasAttribute('data-open')).toBe(true);
  expect(screen.queryByRole('dialog', { name: 'Settings' })).toBeNull();
  expect(screen.getByRole('dialog', { name: 'Pending' })).toBeTruthy();
  expect(overlay.parentElement?.inert).toBe(true);
  await act(async () => slideDone());
  expect(panel.isConnected).toBe(false);
  const drawerDone = transitionOn(screen.getByRole('dialog'));
  rerender(dialogs());
  expect(layer.hasAttribute('data-open')).toBe(false);
  rerender(dialogs('Settings'));
  await act(async () => drawerDone());
  expect(container.querySelector('.modal-layer')).toBe(layer);
  expect(layer.hasAttribute('data-open')).toBe(true);
});

it('reopens immediately even while an earlier dismissal is finishing', async () => {
  enableMotion();
  const { rerender } = render(dialogs('Settings'));
  const previous = screen.getByRole('dialog');
  const finish = transitionOn(previous);
  rerender(dialogs());
  rerender(dialogs('Settings'));
  const current = screen.getByRole('dialog');
  expect(current).not.toBe(previous);
  expect(previous.isConnected).toBe(false);
  await act(async () => finish());
  expect(screen.getByRole('dialog')).toBe(current);
});

it('dismisses immediately when the user prefers reduced motion', () => {
  const { rerender } = render(dialogs('Settings'));
  const panel = screen.getByRole('dialog');
  transitionOn(panel);
  rerender(dialogs());
  expect(panel.isConnected).toBe(false);
});
