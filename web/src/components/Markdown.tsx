import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';

function CodeBlock({ language, content }: { language: string; content: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard may be unavailable on plain HTTP; keep the code visible.
    }
  };
  return (
    <div className="code-block">
      <div className="code-head">
        <span>{language || 'text'}</span>
        <button type="button" onClick={copy}>{copied ? 'Copied' : 'Copy'}</button>
      </div>
      <pre><code>{content}</code></pre>
    </div>
  );
}

/** react-markdown never renders raw HTML; rehype-sanitize is a second boundary. */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        rehypePlugins={[rehypeSanitize]}
        components={{
          code({ className, children }) {
            const language = /language-([\w+-]+)/.exec(className || '')?.[1] || '';
            const content = String(children).replace(/\n$/, '');
            const block = Boolean(language) || content.includes('\n');
            if (!block) return <code className={className}>{children}</code>;
            return <CodeBlock language={language} content={content} />;
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
