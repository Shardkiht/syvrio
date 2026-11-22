import { useLayoutEffect, useRef, useState, type CSSProperties, type MutableRefObject } from 'react';
import type { CompactionPhase, ContextUsage } from '../types';
import { isComposing } from '../state/shortcuts';

const compactTokens = new Intl.NumberFormat('en', { notation: 'compact' });

const COMPACTION_LABEL = {
  running: 'Compressing context…',
  done: 'Context compressed',
  failed: 'Context compression failed',
};

export interface MeterReading {
  used: number;
  percent: number;
}

type Usage = ContextUsage | null | undefined;

function useMeterReading(usage: Usage, retained?: MutableRefObject<MeterReading | null>) {
  const local = useRef<MeterReading | null>(null);
  const reading = retained ?? local;
  const used = usage?.used;
  const percent = usage && usage.limit > 0 ? Math.min(100, 100 * usage.used / usage.limit) : 0;
  const unavailable = usage === null;
  const [shown, setShown] = useState<MeterReading | null>(() => reading.current);

  useLayoutEffect(() => {
    const show = (value: MeterReading | null) => {
      reading.current = value;
      setShown(value);
    };
    if (unavailable) { show(null); return; }
    if (used === undefined) return; // Navigation is awaiting the target session's estimate.
    const from = reading.current;
    const to = { used, percent };
    if (from?.used === used && from.percent === percent) return;
    const motion = window.matchMedia?.('(prefers-reduced-motion: no-preference)');
    if (!from || !motion?.matches) { show(to); return; }

    const start = performance.now();
    let frame: number;
    const advance = (now: number) => {
      const elapsed = Math.max(0, (now - start) / 500);
      if (elapsed >= 1 || !motion.matches) { show(to); return; }
      const progress = 1 - (1 - elapsed) ** 3;
      show({
        used: Math.round(from.used + (to.used - from.used) * progress),
        percent: from.percent + (to.percent - from.percent) * progress,
      });
      frame = requestAnimationFrame(advance);
    };
    frame = requestAnimationFrame(advance);
    return () => cancelAnimationFrame(frame);
  }, [used, percent, unavailable, reading]);
  return shown;
}

export function ContextMeter({ usage, compaction, reading }: {
  usage: Usage;
  compaction: CompactionPhase | null;
  reading?: MutableRefObject<MeterReading | null>;
}) {
  const shown = useMeterReading(usage, reading);
  const [open, setOpen] = useState(false);
  const orb = useRef<HTMLSpanElement>(null);
  const rebound = useRef<Animation | undefined>(undefined);
  const ratio = usage && usage.limit > 0 ? usage.used / usage.limit : 0;
  const percent = shown?.percent ?? 0;
  const actualPercent = Math.min(100, ratio * 100);
  const share = actualPercent > 0 && actualPercent < 1 ? '<1%' : `${Math.floor(actualPercent)}%`;
  const parts = shown ? compactTokens.formatToParts(shown.used) : [];
  const amount = shown ? parts.filter(part => part.type !== 'compact').map(part => part.value).join('') : '—';
  const unit = parts.find(part => part.type === 'compact')?.value.toLowerCase();
  const nearLimit = usage && usage.limit > 0 &&
    usage.used >= (usage.compaction_trigger_tokens ?? usage.limit * usage.compact_threshold);
  const waveCeiling = compaction === 'running' ? 99 : 85;
  const detail = usage
    ? `${usage.used.toLocaleString()} / ${usage.limit.toLocaleString()} tokens (${share})`
    : usage === undefined ? 'Loading estimate…' : 'Estimate unavailable';
  return (
    <div className={`context-meter${open ? ' expanded' : ''}`} data-compaction={compaction ?? undefined}>
      <button type="button" className="context-dial" aria-expanded={open}
        aria-label={`Context estimate: ${detail}`}
        data-fill={percent === 0 ? 'empty' : percent === 100 ? 'full' : 'partial'}
        data-wave={percent >= 10 && percent <= waveCeiling ? 'moving' : 'still'}
        style={{ '--context-fill': `${percent}%` } as CSSProperties}
        onPointerDown={() => rebound.current?.cancel()}
        onClick={() => {
          setOpen(value => !value);
          if (!window.matchMedia?.('(prefers-reduced-motion: no-preference)').matches) return;
          rebound.current?.cancel();
          rebound.current = orb.current?.animate?.(
            [{ scale: '.92' }, { scale: '1.045', offset: .58 }, { scale: '1' }],
            { duration: 240, easing: 'ease-out' },
          );
        }}
        onBlur={() => setOpen(false)}
        onKeyDown={(event) => {
          if (!open || event.key !== 'Escape' || isComposing(event)) return;
          event.preventDefault();
          event.stopPropagation();
          setOpen(false);
        }}>
        <span className="context-orb" ref={orb} aria-hidden="true">
          <svg className="context-ring" viewBox="0 0 32 32" fill="none" focusable="false">
            <circle cx="16" cy="16" r="15" stroke="currentColor" strokeWidth="2" />
          </svg>
          <span className="context-value">
            {amount}{unit && <small>{unit}</small>}
          </span>
        </span>
      </button>
      <div className="context-detail" aria-hidden="true">
        <strong>{compaction ? COMPACTION_LABEL[compaction] : 'Context estimate'}</strong>
        <span>{detail}</span>
        {nearLimit && <span className="context-warning">Near the compaction threshold</span>}
      </div>
      {compaction && <span className="sr-only" role="status">{COMPACTION_LABEL[compaction]}</span>}
    </div>
  );
}
