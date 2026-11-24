/** 主题可跟随浏览器（默认），也可固定使用浅色或深色。 */

export type ThemeMode = 'auto' | 'light' | 'dark';

const STORAGE_KEY = 'syvrio.theme';

const DARK_BG = '#292c32';
const LIGHT_BG = '#f6f7f9';

export function readTheme(): ThemeMode {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    if (value === 'light' || value === 'dark' || value === 'auto') return value;
  } catch {
    // 隐私模式或存储不可用时沿用系统主题。
  }
  return 'auto';
}

function systemPrefersDark(): boolean {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
}

/**
 * 将主题偏好应用到当前文档。
 *
 * 自动模式移除主题覆盖，由 styles.css 中的 prefers-color-scheme 决定配色。
 * 明确选定主题时，:root[data-theme=...] 优先于媒体查询，地址栏也同步使用所选颜色。
 *
 * 启动时以 persist: false 恢复原有偏好，避免写入用户从未选择的值。
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
    // 保存失败也可继续使用当前视图已生效的主题。
  }
}

/** 系统切换配色时发出通知，使自动模式下的界面同步更新。 */
export function watchSystemTheme(onChange: () => void): () => void {
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  if (!media?.addEventListener) return () => {};
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}
