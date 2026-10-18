type MarkdownNode = {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
  data?: Record<string, unknown>;
  position?: { start?: { offset?: number }; end?: { offset?: number } };
};

type ClaimMarker = { start: number; end: number; id: string };

export type AnswerContentOptions = { isLocalFile: (value: string) => boolean };

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function markdownNode(value: unknown): value is MarkdownNode {
  return record(value) && typeof value.type === 'string';
}

function sourceText(node: MarkdownNode, source: string): string | undefined {
  const start = node.position?.start?.offset;
  const end = node.position?.end?.offset;
  if (
    typeof start !== 'number' ||
    typeof end !== 'number' ||
    !Number.isSafeInteger(start) ||
    !Number.isSafeInteger(end) ||
    start < 0 ||
    end < start ||
    end > source.length
  )
    return;
  return source.slice(start, end);
}

function claimMarkers(value: string, raw = false): ClaimMarker[] {
  const markers: ClaimMarker[] = [];
  for (const match of value.matchAll(/\[claim:((?:[^[\]]|\[\d+\])+)\]/g)) {
    if (raw) {
      let slashes = 0;
      let before = match.index;
      while (before > 0 && value[--before] === '\\') slashes += 1;
      if (slashes % 2) continue;
    }
    const id = match[1].trim();
    if (id) markers.push({ start: match.index, end: match.index + match[0].length, id });
  }
  return markers;
}

/** Project authored source markers after Markdown has identified literal code. */
export function remarkAnswerContent({ isLocalFile }: AnswerContentOptions) {
  return (tree: unknown, file: unknown): void => {
    if (!markdownNode(tree) || !record(file) || typeof file.value !== 'string') return;
    const source = file.value;
    const citations = new Map<string, number>();

    function projectClaims(node: MarkdownNode): MarkdownNode[] {
      if (typeof node.value !== 'string') return [node];
      const raw = sourceText(node, source);
      if (raw === undefined) return [node];
      const authored = claimMarkers(raw, true);
      const decoded = claimMarkers(node.value);
      // Entity decoding and escapes can create marker-shaped source literals.
      // Only identical raw and decoded marker sequences may become controls.
      if (
        !authored.length ||
        authored.length !== decoded.length ||
        authored.some((marker, index) => marker.id !== decoded[index].id)
      )
        return [node];
      const projected: MarkdownNode[] = [];
      let start = 0;
      for (const marker of decoded) {
        if (marker.start > start)
          projected.push({ type: 'text', value: node.value.slice(start, marker.start) });
        let index = citations.get(marker.id);
        if (index === undefined) {
          index = citations.size + 1;
          citations.set(marker.id, index);
        }
        projected.push({
          type: 'link',
          url: `#citation:${index}:${encodeURIComponent(marker.id)}`,
          data: { hProperties: { dataAnswerCitation: 'true' } },
          children: [{ type: 'text', value: String(index) }],
        });
        start = marker.end;
      }
      if (start < node.value.length)
        projected.push({ type: 'text', value: node.value.slice(start) });
      return projected;
    }

    function hiddenParagraph(node: MarkdownNode): boolean {
      if (node.type !== 'paragraph') return false;
      const raw = sourceText(node, source)?.trim();
      return Boolean(
        raw &&
        raw
          .split(/\r?\n/)
          .every(
            (line) =>
              /^(?:https?:\/\/|\/|artifacts\/)/i.test(line.trim()) && isLocalFile(line.trim()),
          ),
      );
    }

    function project(node: MarkdownNode, insideLink = false): void {
      if (!Array.isArray(node.children)) return;
      node.children = node.children.flatMap((child) => {
        if (!markdownNode(child)) return [child];
        if (
          child.type === 'html' &&
          typeof child.value === 'string' &&
          /^<!-- evidence-table:[a-f0-9]{24}:(?:start|end) -->(?:\r?\n)?$/.test(child.value)
        )
          return [];
        if (hiddenParagraph(child)) return [];
        if (!insideLink && child.type === 'text') return projectClaims(child);
        if (!['code', 'inlineCode', 'html'].includes(child.type))
          project(child, insideLink || child.type === 'link' || child.type === 'linkReference');
        return [child];
      });
    }

    project(tree);
  };
}
