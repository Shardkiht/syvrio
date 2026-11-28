import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
  window.sessionStorage.removeItem('syvrio.session');
});

// jsdom 未实现 Element.scrollTo。组件测试在需要区分立即滚动和平滑滚动时，
// 将此空实现替换为可记录调用的函数。
Object.defineProperty(HTMLElement.prototype, 'scrollTo', {
  configurable: true,
  writable: true,
  value: () => {},
});

// jsdom 也未实现 Element.scrollIntoView。命令列表用它保持键盘选中项可见，
// 组件测试会替换此空实现以核对滚动目标和参数。
Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
  configurable: true,
  writable: true,
  value: () => {},
});

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: query === '(pointer: fine)',
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});
