import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

/** Markdown 渲染：GFM 表格已按终端密度做样式（见 index.css .md） */
export const MarkdownView = memo(function MarkdownView({
  markdown,
  className,
}: {
  markdown: string;
  className?: string;
}) {
  return (
    <div className={cn("md", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
});
