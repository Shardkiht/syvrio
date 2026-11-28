export function literalPattern(query: string, caseMode: 'unicode' | 'ascii' = 'unicode') {
  const needle = query.trim();
  if (!needle) return null;
  const literal = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // SQLite's lower() folds ASCII letters only.
  const pattern = caseMode === 'ascii'
    ? literal.replace(/[a-z]/gi, ch => '[' + ch.toLowerCase() + ch.toUpperCase() + ']')
    : literal;
  return new RegExp('(' + pattern + ')', caseMode === 'ascii' ? 'u' : 'iu');
}

export function Highlight({ text, pattern }: { text: string; pattern: RegExp | null }) {
  if (!pattern) return <>{text}</>;
  return <>{text.split(pattern).map((part, i) => i % 2 ? <mark key={i}>{part}</mark> : part)}</>;
}
