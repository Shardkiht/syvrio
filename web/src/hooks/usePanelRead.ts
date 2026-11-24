import { useEffect, useRef, useState } from 'react';

/** 以当前展示的条目或查询作为面板的标识。 */
export function usePanelRead<T>(initial: T | undefined, read: (signal: AbortSignal) => Promise<T>, enabled = true) {
  const [loaded, setLoaded] = useState<T>();
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const reader = useRef(read);
  useEffect(() => { reader.current = read; }, [read]);
  useEffect(() => {
    if (!enabled || initial !== undefined) return;
    const controller = new AbortController();
    reader.current(controller.signal).then(
      value => { if (!controller.signal.aborted) setLoaded(value); },
      reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); },
    );
    return () => controller.abort();
  }, [initial, attempt, enabled]);
  function retry() {
    setLoaded(undefined);
    setError(null);
    setAttempt(n => n + 1);
  }
  return { value: initial ?? loaded, error, retry };
}
