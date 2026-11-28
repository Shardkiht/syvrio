import { useEffect, useState, type CSSProperties, type ReactNode } from 'react';

const DEFAULT_WIDTH = 300;
const MIN_WIDTH = 240;
const MAX_WIDTH = 480;
const MIN_CHAT_WIDTH = 440;
const STORAGE_KEY = 'syvrio.sidebar.width';

function readWidth() {
  try {
    const width = Number(localStorage.getItem(STORAGE_KEY));
    if (Number.isInteger(width) && width >= MIN_WIDTH && width <= MAX_WIDTH) return width;
  } catch { /* 浏览器可能已禁用本地存储。 */ }
  return DEFAULT_WIDTH;
}

type Drag = { pointerId: number; startX: number; startWidth: number; width: number };

export function ResizableSidebar({ open, children }: { open: boolean; children: ReactNode }) {
  const [preferred, setPreferred] = useState(readWidth);
  const [viewport, setViewport] = useState(() => window.innerWidth);
  const [drag, setDrag] = useState<Drag | null>(null);
  const resizable = open && viewport >= 768;
  const max = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, viewport - MIN_CHAT_WIDTH));
  const clamp = (value: number) => Math.max(MIN_WIDTH, Math.min(max, Math.round(value)));
  const width = clamp(drag?.width ?? preferred);

  useEffect(() => {
    const resize = () => setViewport(window.innerWidth);
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);
  useEffect(() => { if (!resizable) setDrag(null); }, [resizable]);

  function remember(value: number) {
    setPreferred(value);
    try { localStorage.setItem(STORAGE_KEY, String(value)); }
    catch { /* 本地存储不可用时，当前标签页仍保留所选宽度。 */ }
  }

  return <aside id="session-sidebar" className={`sidebar${open ? ' open' : ''}`}
    style={{ '--sidebar-w': `${width}px` } as CSSProperties} aria-hidden={!open}
    ref={node => { node?.toggleAttribute('inert', !open); }} data-testid="sidebar">
    {children}
    {resizable && <div className="sidebar-divider" role="separator" tabIndex={0}
      aria-label="Chat sidebar width" aria-orientation="vertical" aria-controls="session-sidebar"
      aria-valuemin={MIN_WIDTH} aria-valuemax={max} aria-valuenow={width} aria-valuetext={`${width} pixels`}
      data-dragging={!!drag || undefined}
      onPointerDown={event => {
        if (drag || !event.isPrimary || event.button !== 0) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        setDrag({ pointerId: event.pointerId, startX: event.clientX, startWidth: width, width });
      }}
      onPointerMove={event => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        setDrag({ ...drag, width: clamp(drag.startWidth + event.clientX - drag.startX) });
      }}
      onPointerUp={event => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        const next = clamp(drag.startWidth + event.clientX - drag.startX);
        if (next !== drag.startWidth) remember(next);
        setDrag(null);
      }}
      onLostPointerCapture={event => {
        if (drag?.pointerId === event.pointerId) setDrag(null);
      }}
      onDoubleClick={() => remember(DEFAULT_WIDTH)}
      onKeyDown={event => {
        const targets: Record<string, number> = {
          ArrowLeft: width - 10, ArrowRight: width + 10, Home: MIN_WIDTH, End: max,
        };
        if (drag || !(event.key in targets)) return;
        event.preventDefault();
        remember(clamp(targets[event.key]));
      }} />}
  </aside>;
}
