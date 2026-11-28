import { describe, expect, it } from 'vitest';
import { formatLocalTimestamp } from './time';

describe('formatLocalTimestamp', () => {
  it.each([
    ['2026-11-11T00:00:00+00:00', 'UTC', '2026-11-11 00:00'],
    ['2026-11-11T12:00:00+00:00', 'UTC', '2026-11-11 12:00'],
    ['2026-11-11T01:05:03+00:00', 'Asia/Shanghai', '2026-11-11 09:05'],
    ['2026-11-11T22:01:00+00:00', 'Asia/Shanghai', '2026-11-12 06:01'],
    ['2026-11-11T02:01:00+00:00', 'America/New_York', '2026-11-10 21:01'],
  ])('displays %s in %s with 24-hour local time', (value, zone, expected) => {
    expect(formatLocalTimestamp(value, zone)).toBe(expected);
  });

  it('rejects a timestamp without an offset', () => {
    expect(() => formatLocalTimestamp('2026-11-11T09:05:03')).toThrow(RangeError);
  });

  it('uses the current system zone for local times, also across offset transitions', () => {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    for (const stamp of ['2026-03-08T06:59:00Z', '2026-03-08T07:01:00Z', '2026-11-01T06:05:00Z']) {
      expect(formatLocalTimestamp(stamp)).toBe(formatLocalTimestamp(stamp, zone));
    }
    expect(() => formatLocalTimestamp('invalidZ')).toThrow(RangeError);
  });
});
