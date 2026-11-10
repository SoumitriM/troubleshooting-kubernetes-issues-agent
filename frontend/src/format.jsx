// Renders the assistant's plain-text answers: paragraphs, "-" and "1." lists,
// and **bold** spans. Builds React elements only, so answer text can never
// inject HTML.

function inline(text, keyPrefix) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, index) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4
      ? <strong key={`${keyPrefix}-${index}`}>{part.slice(2, -2)}</strong>
      : part,
  );
}

export function FormattedAnswer({ text }) {
  const blocks = [];
  let list = null;
  let paragraph = [];

  const flushParagraph = () => {
    if (paragraph.length) blocks.push({ type: "p", lines: paragraph });
    paragraph = [];
  };
  const flushList = () => {
    if (list) blocks.push(list);
    list = null;
  };

  for (const rawLine of text.split("\n")) {
    const line = rawLine.trimEnd();
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      flushParagraph();
      const type = bullet ? "ul" : "ol";
      if (!list || list.type !== type) {
        flushList();
        list = { type, items: [] };
      }
      list.items.push((bullet || numbered)[1]);
    } else if (!line.trim()) {
      flushParagraph();
      flushList();
    } else {
      flushList();
      paragraph.push(line);
    }
  }
  flushParagraph();
  flushList();

  return (
    <div className="formatted">
      {blocks.map((block, index) => {
        if (block.type === "p") {
          return (
            <p key={index}>
              {block.lines.map((line, lineIndex) => (
                <span key={lineIndex}>
                  {lineIndex > 0 && <br />}
                  {inline(line, `${index}-${lineIndex}`)}
                </span>
              ))}
            </p>
          );
        }
        const List = block.type;
        return (
          <List key={index}>
            {block.items.map((item, itemIndex) => (
              <li key={itemIndex}>{inline(item, `${index}-${itemIndex}`)}</li>
            ))}
          </List>
        );
      })}
    </div>
  );
}

export function sourceLabel(url) {
  try {
    const parsed = new URL(url);
    const issue = parsed.pathname.match(/\/(issues|pull)\/(\d+)/);
    if (parsed.hostname === "github.com" && issue) {
      return `${issue[1] === "pull" ? "PR" : "Issue"} #${issue[2]}`;
    }
    const segments = parsed.pathname.split("/").filter(Boolean);
    const last = segments.at(-1) || parsed.hostname;
    return last.replaceAll("-", " ");
  } catch {
    return url;
  }
}

export function relativeTime(iso, now = Date.now()) {
  const seconds = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604_800) return `${Math.floor(seconds / 86_400)}d ago`;
  return new Date(iso).toLocaleDateString();
}
