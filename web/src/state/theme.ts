/** Theme preference: follow the browser (default) or pin light/dark. */

export type ThemeMode = 'auto' | 'light' | 'dark';

const STORAGE_KEY = 'syvrio.theme';

const DARK_BG = '#0e1014';
const LIGHT_BG = '#f6f7f9';

export function readTheme(): ThemeMode {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    if (value === 'light' || value === 'dark' || value === 'auto') return value;
  } catch {
    // Private mode or blocked storage: fall back to the system preference.
  }
  return 'auto';
}

function systemPrefersDark(): boolean {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
}

/**
 * Apply a preference to the document.
 *
 * `auto` removes the override entirely so the `prefers-color-scheme` media
 * query in styles.css decides; an explicit mode wins over the media query via
 * `:root[data-theme=...]`. The address-bar colour follows the same decision.
 *
 * `persist: false` is for the boot path: restoring what was already stored must
 * not write a value the user never chose.
 */
export function applyTheme(mode: ThemeMode, persist = true): void {
  const root = document.documentElement;
  if (mode === 'auto') root.removeAttribute('data-theme');
  else root.dataset.theme = mode;
  const dark = mode === 'auto' ? systemPrefersDark() : mode === 'dark';
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', dark ? DARK_BG : LIGHT_BG);
  if (!persist) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // Persisting is best effort; the current view is already correct.
  }
}

/** Notify when the OS switches scheme, so `auto` can keep the chrome in sync. */
export function watchSystemTheme(onChange: () => void): () => void {
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  if (!media?.addEventListener) return () => {};
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}
