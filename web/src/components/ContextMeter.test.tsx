import { act, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ContextUsage } from '../types';
import { ContextMeter, type MeterReading } from './ContextMeter';

function estimate(session: string | null, used: number): ContextUsage {
  return { session_id: session, used, limit: 1000, compact_threshold: 0.85 };
}

afterEach(() => vi.restoreAllMocks());

it('warns at the server trigger and falls back to the ratio for older servers', () => {
  const usage = estimate('a', 699);
  const meter = (value: ContextUsage) => <ContextMeter usage={value} compaction={null} />;
  const warning = () => screen.queryByText('Near the compaction threshold');
  const { rerender } = render(meter({ ...usage, compaction_trigger_tokens: 700 }));
  expect(warning()).toBeNull();
  rerender(meter({ ...usage, used: 700, compaction_trigger_tokens: 700 }));
  expect(warning()).toBeTruthy();
  rerender(meter({ ...usage, used: 849 }));
  expect(warning()).toBeNull();
  rerender(meter({ ...usage, used: 850 }));
  expect(warning()).toBeTruthy();
});

it('retains the visible frame while switching, then animates toward the latest session, including zero', () => {
  const media = window.matchMedia('(prefers-reduced-motion: no-preference)');
  vi.spyOn(window, 'matchMedia').mockReturnValue({ ...media, matches: true });
  let now = 0;
  let nextId = 1;
  const callbacks = new Map<number, FrameRequestCallback>();
  vi.spyOn(performance, 'now').mockImplementation(() => now);
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => {
    const id = nextId++;
    callbacks.set(id, callback);
    return id;
  });
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(id => { callbacks.delete(id); });
  const advance = (time: number) => {
    now = time;
    const frame = [...callbacks.values()];
    callbacks.clear();
    act(() => frame.forEach(callback => callback(now)));
  };
  const reading = { current: null as MeterReading | null };
  const meter = (key: string, usage?: ContextUsage) =>
    <ContextMeter key={key} usage={usage} reading={reading} compaction={null} />;
  const dial = () => screen.getByRole<HTMLButtonElement>('button', { name: /Context estimate/ });
  const { rerender } = render(meter('a', estimate('a', 800)));
  expect(dial().textContent).toBe('80%');
  rerender(meter('b'));
  expect(dial().textContent).toBe('80%');
  expect(dial().getAttribute('aria-label')).toContain('Loading estimate');
  rerender(meter('b', estimate('b', 200)));
  advance(125);
  const intermediate = Number.parseInt(dial().textContent!, 10);
  expect(intermediate).toBeGreaterThan(20);
  expect(intermediate).toBeLessThan(80);
  const water = Number.parseFloat(dial().style.getPropertyValue('--context-fill'));
  expect(Math.floor(water)).toBe(intermediate);

  rerender(meter('new'));
  advance(250);
  expect(Number.parseInt(dial().textContent!, 10)).toBe(intermediate);
  expect(dial().style.getPropertyValue('--context-fill')).toBe(`${water}%`);
  rerender(meter('new', estimate(null, 0)));
  advance(350);
  expect(Number.parseInt(dial().textContent!, 10)).toBeGreaterThan(0);
  expect(Number.parseInt(dial().textContent!, 10)).toBeLessThan(intermediate);
  advance(750);
  expect(dial().textContent).toBe('0%');
  expect(dial().dataset.fill).toBe('empty');
  expect(callbacks.size).toBe(0);
});

it('applies exact readings immediately under reduced motion and clears a failed estimate', () => {
  const media = window.matchMedia('(prefers-reduced-motion: no-preference)');
  vi.spyOn(window, 'matchMedia').mockReturnValue({ ...media, matches: false });
  const { rerender } = render(<ContextMeter usage={estimate('a', 140)} compaction={null} />);
  rerender(<ContextMeter usage={estimate('b', 610)} compaction={null} />);
  const dial = screen.getByRole('button', { name: /610.*61%/ });
  expect(dial.textContent).toBe('61%');
  expect(dial.style.getPropertyValue('--context-fill')).toBe('61%');
  rerender(<ContextMeter usage={null} compaction={null} />);
  expect(dial.textContent).toBe('—');
  expect(dial.getAttribute('aria-label')).toContain('Estimate unavailable');
});
