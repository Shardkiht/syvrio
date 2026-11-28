let lastZone: string | undefined;
let lastFormatter: Intl.DateTimeFormat | undefined;

function inZone(zone: string): Intl.DateTimeFormat {
  if (!lastFormatter || zone !== lastZone) {
    lastFormatter = new Intl.DateTimeFormat('en-CA', {
      timeZone: zone, calendar: 'gregory', numberingSystem: 'latn',
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    });
    lastZone = zone;
  }
  return lastFormatter;
}

// 本地字段跟随系统当前时区；显式时区复用最近一次的格式器。
export function formatLocalTimestamp(value: string, timeZone?: string): string {
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    throw new RangeError('Timestamp requires an explicit UTC offset');
  }
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new RangeError('Invalid timestamp');
  if (timeZone === undefined) {
    const pad = (part: number) => String(part).padStart(2, '0');
    const year = date.getFullYear();
    return String(year > 0 ? year : 1 - year).padStart(4, '0') + '-' + pad(date.getMonth() + 1)
      + '-' + pad(date.getDate()) + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  }
  const fields = Object.fromEntries(inZone(timeZone).formatToParts(date).map(({ type, value }) => [type, value]));
  return fields.year.padStart(4, '0') + '-' + fields.month + '-' + fields.day
    + ' ' + fields.hour + ':' + fields.minute;
}
