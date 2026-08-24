import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Props = {
  value: string;
  ariaLabel: string;
};

/**
 * Rendered Markdown is intentionally provided by the maintained CommonMark/GFM
 * stack. Monaco remains the source editor; it does not provide a rendered
 * Markdown preview in the standalone editor package.
 */
export default function ProjectMarkdownPreview({ value, ariaLabel }: Props) {
  return (
    <article
      className="project-file-workspace__markdown-preview"
      aria-label={ariaLabel}
      data-markdown-preview="rendered"
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{value}</ReactMarkdown>
    </article>
  );
}
