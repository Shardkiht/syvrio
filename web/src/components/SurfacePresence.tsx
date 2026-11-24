import {
  Children, isValidElement, useCallback, useLayoutEffect, useReducer, useRef,
  type ReactElement, type ReactNode,
} from 'react';

type Surface = { child: ReactElement; id: number; closing: boolean };

function SurfaceFrame({ surface, dismiss }: { surface: Surface; dismiss: (id: number) => void }) {
  const frame = useRef<HTMLDivElement>(null);
  const { closing, id } = surface;
  useLayoutEffect(() => {
    const node = frame.current;
    if (!node || !closing) return;
    node.inert = true;
    const root = node.firstElementChild;
    // 只由弹层或对话框本身的动效决定何时卸载，内部内容可以持续播放动画。
    const panel = root?.querySelector(':scope > [aria-modal="true"]');
    const animations = [...(root?.getAnimations?.() ?? []), ...(panel?.getAnimations?.() ?? [])];
    let mounted = true;
    void Promise.allSettled(animations.map(animation => animation.finished)).then(() => {
      if (mounted) dismiss(id);
    });
    return () => { mounted = false; };
  }, [closing, id, dismiss]);

  return <div ref={frame} className="surface-frame" data-closing={closing || undefined}
    aria-hidden={closing || undefined}>{surface.child}</div>;
}

/** 关闭请求立即生效，同时保留已挂载的界面以播放退出动画。 */
export function SurfacePresence({ children, modal = false }: { children: ReactNode; modal?: boolean }) {
  const committed = useRef<Surface[]>([]);
  const [, redraw] = useReducer(value => value + 1, 0);
  const previous = committed.current;
  let nextId = Math.max(0, ...previous.map(surface => surface.id)) + 1;
  const active = Children.toArray(children).filter(isValidElement).map(child => {
    const existing = previous.find(item => !item.closing && item.child.key === child.key && item.child.type === child.type);
    return { child, id: existing?.id ?? nextId++, closing: false };
  });
  const closing = window.matchMedia?.('(prefers-reduced-motion: no-preference)').matches
    ? previous.filter(item => !active.some(current => current.child.key === item.child.key))
      .map(item => ({ ...item, closing: true }))
    : [];
  const surfaces = [...closing, ...active];
  useLayoutEffect(() => { committed.current = surfaces; });
  const dismiss = useCallback((id: number) => {
    committed.current = committed.current.filter(item => item.id !== id);
    redraw();
  }, []);
  const frames = surfaces.map(surface => <SurfaceFrame key={surface.id} surface={surface} dismiss={dismiss} />);
  return modal ? <div className="modal-layer" data-open={active.length > 0 || undefined}>{frames}</div> : frames;
}
