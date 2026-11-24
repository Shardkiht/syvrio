// 传输时使用 UTC 绝对时间戳，显示时区由浏览器决定。
export function formatLocalTimestamp(value: string, timeZone?: string): string {
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    throw new RangeError('Timestamp requires an explicit UTC offset');
  }
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    calendar: 'gregory',
    numberingSystem: 'latn',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(value));
  const fields = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return fields.year.padStart(4, '0') + '-' + fields.month + '-' + fields.day
    + ' ' + fields.hour + ':' + fields.minute;
}
