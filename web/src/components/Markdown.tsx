import { createContext, useContext, useState } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize from 'rehype-sanitize';

import { ImagePreview } from './AttachmentImage';

const InsideLink = createContext(false);

const chatReferences: Components = {
  a({ href, title, children }) {
    return <InsideLink.Provider value={true}><a href={href} title={title}>{children}</a></InsideLink.Provider>;
  },
  img: function MarkdownImage({ src, alt, title }) {
    const linked = useContext(InsideLink);
    return src ? <ImagePreview key={src} src={src} name={alt || 'Image'} title={title} linked={linked} />
      : <span>{alt || 'Image unavailable'}</span>;
  },
};

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

// Read-only documents can cite links and images without navigating or loading them.
const readingReferences: Components = {
  a({ href, children }) {
    return <span>{children}{href && children !== href && <span className="reading-reference"> ({href})</span>}</span>;
  },
  img({ src, alt }) {
    return <span>{alt || src || 'Image'}{alt && src && <span className="reading-reference"> ({src})</span>}</span>;
  },
};

/** react-markdown never renders raw HTML; rehype-sanitize is a second boundary. */
export function Markdown({ text, reading = false }: { text: string; reading?: boolean }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        components={{
          ...(reading ? readingReferences : chatReferences),
          code({ className, children }) {
            if (reading) return <code className={className}>{children}</code>;
            const language = /language-([\w+-]+)/.exec(className || '')?.[1] || '';
            const content = String(children).replace(/\n$/, '');
            const block = Boolean(language) || content.includes('\n');
            if (!block) return <code className={className}>{children}</code>;
            return <CodeBlock language={language} content={content} />;
          },
          table({ children }) {
            return (
              <div className="table-wrap">
                <table>{children}</table>
              </div>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
