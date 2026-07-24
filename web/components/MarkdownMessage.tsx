"use client";

import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import type { Components } from "react-markdown";

const components: Components = {
  p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
  ul: ({ children }) => (
    <ul className="mb-2 list-disc space-y-1 pl-4 last:mb-0">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-2 list-decimal space-y-1 pl-4 last:mb-0">{children}</ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  strong: ({ children }) => (
    <strong className="font-semibold text-cloud">{children}</strong>
  ),
  em: ({ children }) => <em className="italic text-cloud/90">{children}</em>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline underline-offset-2 hover:opacity-90"
    >
      {children}
    </a>
  ),
  code: ({ className, children, ...props }) => {
    const isBlock = Boolean(className?.includes("language-"));
    if (isBlock) {
      return (
        <code
          className={[
            "my-2 block overflow-x-auto rounded-lg bg-ink-950/80 px-2.5 py-2",
            "font-mono text-[12px] text-cloud/90",
            className,
          ]
            .filter(Boolean)
            .join(" ")}
          {...props}
        >
          {children}
        </code>
      );
    }
    return (
      <code
        className="rounded bg-ink-950/70 px-1 py-0.5 font-mono text-[12px] text-cloud/90"
        {...props}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded-lg bg-ink-950/80 p-0 last:mb-0">
      {children}
    </pre>
  ),
  h1: ({ children }) => (
    <h1 className="mb-2 text-base font-semibold text-cloud">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="mb-2 text-sm font-semibold text-cloud">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="mb-1.5 text-sm font-semibold text-cloud">{children}</h3>
  ),
  hr: () => <hr className="my-3 border-ink-700/80" />,
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-violet/40 pl-3 text-slate-400">
      {children}
    </blockquote>
  ),
};

/** Renders assistant chat content as Markdown with KaTeX math ($...$ / $$...$$). */
export function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="chat-md break-words text-sm leading-relaxed [&_.katex]:text-[0.95em] [&_.katex-display]:my-2 [&_.katex-display]:overflow-x-auto">
      <ReactMarkdown
        remarkPlugins={[remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
