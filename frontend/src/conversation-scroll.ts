type ConversationViewport = {
  scrollY: number;
  viewportTop: number;
  viewportHeight: number;
  composerTop: number;
  tailBottom: number;
  documentHeight: number;
  layoutHeight: number;
};

// Coordinates are viewport-relative. The composer and an on-screen keyboard can
// each hide content that would otherwise look like the document's bottom.
export function conversationScrollPosition(view: ConversationViewport) {
  const visibleBottom = view.viewportTop + view.viewportHeight;
  const usableBottom = Math.max(view.viewportTop, Math.min(visibleBottom, view.composerTop) - 20);
  const distance = Math.max(0, view.tailBottom - usableBottom);
  return {
    nearBottom: distance <= 100,
    target: Math.max(
      0,
      Math.min(
        view.documentHeight - view.layoutHeight,
        view.scrollY + view.tailBottom - usableBottom,
      ),
    ),
  };
}
