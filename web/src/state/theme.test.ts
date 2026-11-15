import { afterEach, describe, expect, it, vi } from 'vitest';
import { applyTheme, readTheme, watchSystemTheme } from './theme';

function mockSystemTheme(initialDark: boolean) {
  let dark = initialDark;
  const listeners = new Set<() => void>();
  const media = {
    get matches() { return dark; },
    addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
  } as unknown as MediaQueryList;
  vi.stubGlobal('matchMedia', vi.fn(() => media));
  return {
    setDark(value: boolean) {
      dark = value;
      listeners.forEach((listener) => listener());
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});

describe('theme preference', () => {
  it('persists an explicit theme and updates the browser chrome', () => {
    document.head.innerHTML = '<meta name="theme-color" content="" />';
    mockSystemTheme(false);

    applyTheme('dark');

    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe('#0e1014');
    expect(readTheme()).toBe('dark');
  });

  it('follows live system changes in auto mode without a reload', () => {
    document.head.innerHTML = '<meta name="theme-color" content="" />';
    const system = mockSystemTheme(true);
    applyTheme('auto');
    const unwatch = watchSystemTheme(() => applyTheme('auto'));

    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe('#0e1014');
    system.setDark(false);
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe('#f6f7f9');

    unwatch();
    system.setDark(true);
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe('#f6f7f9');
  });
});
