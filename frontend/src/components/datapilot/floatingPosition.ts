export type Offset = {
  x: number;
  y: number;
};

export type ViewportSize = {
  width: number;
  height: number;
};

type FloatingSize = {
  width: number;
  height: number;
};

const VIEWPORT_MARGIN = 16;
const DESKTOP_EDGE_GAP = 20;
const MOBILE_EDGE_GAP = 12;
const FLOATING_BUTTON_WIDTH = 208;
const FLOATING_BUTTON_HEIGHT = 56;
const MAX_WINDOW_WIDTH = 500;
const MAX_WINDOW_HEIGHT = 680;

export const DEFAULT_FLOATING_BUTTON_SIZE = {
  width: FLOATING_BUTTON_WIDTH,
  height: FLOATING_BUTTON_HEIGHT,
};

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function edgeGapFor(viewport: ViewportSize) {
  return viewport.width >= 640 ? DESKTOP_EDGE_GAP : MOBILE_EDGE_GAP;
}

export function currentViewport(): ViewportSize {
  return {
    width: window.innerWidth,
    height: window.innerHeight,
  };
}

export function visibleFloatingOffset(
  offset: Offset,
  viewport: ViewportSize,
  floatingSize: FloatingSize = DEFAULT_FLOATING_BUTTON_SIZE,
): Offset {
  const edgeGap = edgeGapFor(viewport);
  const baseLeft = viewport.width - edgeGap - floatingSize.width;
  const baseTop = viewport.height - edgeGap - floatingSize.height;
  const minX = VIEWPORT_MARGIN - baseLeft;
  const maxX = viewport.width - VIEWPORT_MARGIN - floatingSize.width - baseLeft;
  const minY = VIEWPORT_MARGIN - baseTop;
  const maxY = viewport.height - VIEWPORT_MARGIN - floatingSize.height - baseTop;

  return {
    x: clamp(offset.x, minX, maxX),
    y: clamp(offset.y, minY, maxY),
  };
}

export function visibleWindowOffset(anchorOffset: Offset, viewport: ViewportSize): Offset {
  const edgeGap = edgeGapFor(viewport);
  const windowWidth = Math.min(MAX_WINDOW_WIDTH, viewport.width - edgeGap * 2);
  const windowHeight = Math.min(MAX_WINDOW_HEIGHT, viewport.height - edgeGap * 2);
  const baseLeft = viewport.width - edgeGap - windowWidth;
  const baseTop = viewport.height - edgeGap - windowHeight;
  const minX = VIEWPORT_MARGIN - baseLeft;
  const maxX = viewport.width - VIEWPORT_MARGIN - windowWidth - baseLeft;
  const minY = VIEWPORT_MARGIN - baseTop;
  const maxY = viewport.height - VIEWPORT_MARGIN - windowHeight - baseTop;

  return {
    x: clamp(anchorOffset.x, minX, maxX),
    y: clamp(anchorOffset.y, minY, maxY),
  };
}
