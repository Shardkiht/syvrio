import { useId, useLayoutEffect, useRef, useState, type CSSProperties, type MutableRefObject } from 'react';
import type { CompactionPhase, ContextUsage } from '../../types';

function formatPercent(value: number) {
  return value > 0 && value < 1 ? '<1%' : `${Math.floor(value)}%`;
}

const COMPACTION_LABEL = {
  running: 'Compressing context…',
  done: 'Context compressed',
  failed: 'Context compression failed',
};

export interface MeterReading {
  percent: number;
  fill: number;
}

type Usage = ContextUsage | null | undefined;

function useMeterReading(target: number | null | undefined, newChat: boolean, retained?: MutableRefObject<MeterReading | null>) {
  const local = useRef<MeterReading | null>(null);
  const reading = retained ?? local;
  const [shown, setShown] = useState<MeterReading | null>(() => reading.current);

  useLayoutEffect(() => {
    const show = (value: MeterReading | null) => {
      reading.current = value;
      setShown(value);
    };
    if (target === null) { show(null); return; }
    if (target === undefined) return; // 切换时等待目标会话返回用量估算。
    const from = reading.current;
    const to = { percent: target, fill: target };
    if (from?.percent === target && from.fill === target) return;
    const motion = window.matchMedia?.('(prefers-reduced-motion: no-preference)');
    if (!from || !motion?.matches) { show(to); return; }

    // 新会话的用量已经归零，只有水位需要从旧高度下降。
    const startPercent = newChat ? target : from.percent;
    if (startPercent !== from.percent) show({ percent: startPercent, fill: from.fill });
    const start = performance.now();
    let frame: number;
    const advance = (now: number) => {
      const elapsed = Math.max(0, (now - start) / 500);
      if (elapsed >= 1 || !motion.matches) { show(to); return; }
      const progress = 1 - (1 - elapsed) ** 3;
      show({
        percent: startPercent + (target - startPercent) * progress,
        fill: from.fill + (target - from.fill) * progress,
      });
      frame = requestAnimationFrame(advance);
    };
    frame = requestAnimationFrame(advance);
    return () => cancelAnimationFrame(frame);
  }, [target, newChat, reading]);
  return shown;
}

export function ContextMeter({ usage, compaction, reading, newChat = false }: {
  usage: Usage;
  compaction: CompactionPhase | null;
  reading?: MutableRefObject<MeterReading | null>;
  newChat?: boolean;
}) {
  const percent = usage && (usage.limit > 0 ? Math.min(100, 100 * usage.used / usage.limit) : 0);
  const shown = useMeterReading(newChat ? 0 : percent, newChat, reading);
  const orb = useRef<HTMLSpanElement>(null);
  const rebound = useRef<Animation | undefined>(undefined);
  const fill = shown?.fill ?? 0;
  // 用量较低时加深球底的可见水层。
  const waterHeight = fill < 20 ? Math.sqrt(fill * 20) : fill;
  const waveControl = Math.min(4, 32 * waterHeight / 100);
  const waterId = useId();
  const reflectionId = useId();
  const nearLimit = usage && usage.limit > 0 &&
    usage.used >= (usage.compaction_trigger_tokens ?? usage.limit * usage.compact_threshold);
  const waveCeiling = compaction === 'running' ? 99 : 85;
  const detail = usage
    ? `${usage.used.toLocaleString()} / ${usage.limit.toLocaleString()} tokens (${formatPercent(percent ?? 0)})`
    : newChat ? (usage === null ? '0 tokens (0%) · Limit unavailable' : '0 tokens (0%)')
    : usage === undefined ? 'Loading estimate…' : 'Estimate unavailable';
  return (
    <div className="context-meter" data-compaction={compaction ?? undefined}>
      <button type="button" className="context-dial"
        aria-label={`Context estimate: ${detail}`}
        data-fill={fill === 0 ? 'empty' : fill === 100 ? 'full' : 'partial'}
        data-wave={fill > 0 && fill <= waveCeiling ? 'moving' : 'still'}
        style={{ '--context-fill': `${waterHeight}%` } as CSSProperties}
        onPointerDown={() => rebound.current?.cancel()}
        onClick={() => {
          if (!window.matchMedia?.('(prefers-reduced-motion: no-preference)').matches) return;
          rebound.current?.cancel();
          rebound.current = orb.current?.animate?.(
            [{ scale: '.92' }, { scale: '1.045', offset: .58 }, { scale: '1' }],
            { duration: 240, easing: 'ease-out' },
          );
        }}>
        <span className="context-orb" ref={orb} aria-hidden="true">
          {fill > 0 && (
            <span className="context-liquid">
              <svg className="context-water" viewBox="0 0 64 32" preserveAspectRatio="none" focusable="false">
                <defs>
                  <linearGradient id={waterId} gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="0" y2={32 * waterHeight / 100}>
                    <stop stopColor="#b0d5fa" stopOpacity=".32" />
                    <stop offset=".34" stopColor="#a8c7f6" stopOpacity=".22" />
                    <stop offset="1" stopColor="var(--water-depth)" stopOpacity=".64" />
                  </linearGradient>
                </defs>
                {/* 两个波浪周期共用同一轮廓，让波峰随水面移动。 */}
                <path d={fill === 100 ? 'M0 0h64v36H0Z' : `M0 0q8 -${waveControl} 16 0t16 0t16 0t16 0v36H0Z`}
                  fill={`url(#${waterId})`} vectorEffect="non-scaling-stroke" />
              </svg>
            </span>
          )}
          <svg className="context-ring" viewBox="0 0 32 32" fill="none" focusable="false">
            <defs>
              <linearGradient id={reflectionId} x1="0" y1="0" x2=".55" y2="1">
                <stop offset=".18" stopColor="white" stopOpacity=".96" />
                <stop offset="1" stopColor="white" stopOpacity=".28" />
              </linearGradient>
            </defs>
            <path fill={`url(#${reflectionId})`}
              d="M5.7 12.4c.8-3.9 4.7-7.4 9.5-7.9l-.9 3.4c-3.3.7-5.6 2.2-7.7 5.2Z" />
            <circle cx="16" cy="16" r="15" stroke="currentColor" strokeWidth="2" />
          </svg>
          <span className="context-value">
            {shown ? formatPercent(shown.percent) : '—'}
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
