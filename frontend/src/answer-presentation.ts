export type AnswerPresentationOptions = {
  prefix: string;
  language: 'en' | 'vi';
  interactive?: boolean;
};

type MarkdownNode = {
  type: string;
  value?: string;
  url?: string;
  depth?: number;
  ordered?: boolean;
  spread?: boolean;
  start?: number;
  children?: MarkdownNode[];
  position?: { start?: { line?: number } };
  data?: Record<string, unknown>;
};

const minimumSections = 3;
const minimumReadingLength = 1000;

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function markdownNode(value: unknown): value is MarkdownNode {
  return record(value) && typeof value.type === 'string';
}

function sectionPrefix(prefix: string): string {
  // Encode every code point so distinct React IDs cannot collapse when sanitized.
  const identity = Array.from(prefix, (character) => character.codePointAt(0)?.toString(16)).join(
    '-',
  );
  return `answer-section-${identity || 'root'}-`;
}

export function answerSectionId(prefix: string, ordinal: number): string {
  if (!Number.isSafeInteger(ordinal) || ordinal < 1)
    throw new RangeError('An answer section ordinal must be a positive safe integer');
  return `${sectionPrefix(prefix)}${ordinal}`;
}

export function isAnswerSectionLink(href: unknown, prefix: string): href is string {
  if (typeof href !== 'string') return false;
  const expected = `#${sectionPrefix(prefix)}`;
  if (!href.startsWith(expected)) return false;
  const ordinal = href.slice(expected.length);
  return /^[1-9]\d*$/.test(ordinal) && Number.isSafeInteger(Number(ordinal));
}

function readableText(value: unknown): string {
  if (!markdownNode(value)) return '';
  // Hidden code and source markers cannot make a short answer appear long.
  if (value.type === 'code' || value.type === 'html') return '';
  if (value.type === 'link' && /^#(?:citation|claim):/.test(value.url || '')) return '';
  if (value.type === 'break') return ' ';
  if (typeof value.value === 'string') return value.value;
  const separator = /^(?:root|blockquote|list|listItem|table|tableRow)$/.test(value.type)
    ? ' '
    : '';
  return Array.isArray(value.children) ? value.children.map(readableText).join(separator) : '';
}

function headingLabel(node: MarkdownNode): string {
  return readableText(node).replace(/\s+/g, ' ').trim();
}

function authoredHeading(node: MarkdownNode): boolean {
  return (
    node.type === 'heading' &&
    typeof node.depth === 'number' &&
    Number.isInteger(node.depth) &&
    node.depth >= 1 &&
    node.depth <= 6 &&
    typeof node.position?.start?.line === 'number' &&
    node.position.start.line > 0
  );
}

function navigation(
  sections: { id: string; label: string }[],
  language: AnswerPresentationOptions['language'],
): MarkdownNode {
  const label = language === 'vi' ? 'Trong câu trả lời' : 'In this answer';
  return {
    type: 'blockquote',
    data: {
      answerPresentation: 'outline',
      hName: 'details',
      hProperties: { className: 'answer-outline' },
    },
    children: [
      {
        type: 'paragraph',
        data: { hName: 'summary' },
        children: [{ type: 'text', value: label }],
      },
      {
        type: 'blockquote',
        data: { hName: 'nav', hProperties: { ariaLabel: label } },
        children: [
          {
            type: 'list',
            ordered: true,
            spread: false,
            start: 1,
            data: { hProperties: { className: 'answer-outline-sections' } },
            children: sections.map((section) => ({
              type: 'listItem',
              spread: false,
              children: [
                {
                  type: 'paragraph',
                  children: [
                    {
                      type: 'link',
                      url: `#${section.id}`,
                      children: [{ type: 'text', value: section.label }],
                    },
                  ],
                },
              ],
            })),
          },
        ],
      },
    ],
  };
}

/** Add optional navigation to parsed report Markdown without rewriting its source. */
export function remarkAnswerPresentation({
  prefix,
  language,
  interactive = true,
}: AnswerPresentationOptions) {
  return (tree: unknown): void => {
    if (
      !interactive ||
      !markdownNode(tree) ||
      tree.type !== 'root' ||
      !Array.isArray(tree.children)
    )
      return;

    const children = tree.children.filter(
      (node) => markdownNode(node) && node.data?.answerPresentation !== 'outline',
    );
    // Only primary report sections belong in navigation. Headings inside a quote
    // or list remain part of that authored block, rather than becoming report tabs.
    const headings = children.filter(authoredHeading);
    const documentTitle =
      headings[0]?.depth === 1 && headings.slice(1).every((heading) => (heading.depth || 0) > 1);
    const sections = headings
      .map((heading, index) => ({
        heading,
        id: answerSectionId(prefix, index + 1),
        label: headingLabel(heading),
      }))
      .filter((section, index) => section.label && !(documentTitle && index === 0));
    const readingLength = children.map(readableText).join(' ').replace(/\s+/g, ' ').trim().length;
    if (sections.length < minimumSections || readingLength < minimumReadingLength) return;

    for (const section of sections) {
      const data = record(section.heading.data) ? section.heading.data : {};
      const properties = record(data.hProperties) ? data.hProperties : {};
      section.heading.data = {
        ...data,
        hProperties: { ...properties, id: section.id, tabIndex: -1 },
      };
    }

    // Keep the authored introduction and document title ahead of the controls.
    const firstSection = children.indexOf(sections[0].heading);
    tree.children = [
      ...children.slice(0, firstSection),
      navigation(sections, language),
      ...children.slice(firstSection),
    ];
  };
}
