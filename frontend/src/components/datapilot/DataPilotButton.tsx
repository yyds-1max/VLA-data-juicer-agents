import type { PointerEvent } from "react";
import { useCallback, useEffect, useRef } from "react";
import { Bot, MessageSquareText } from "lucide-react";
import { useStore } from "zustand";

import { datapilotStore } from "../../store/datapilotStore";
import {
  DEFAULT_FLOATING_BUTTON_SIZE,
  currentViewport,
  visibleFloatingOffset,
} from "./floatingPosition";

type FloatingButtonDragState = {
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
  width: number;
  height: number;
};

export function DataPilotButton() {
  const open = useStore(datapilotStore, (state) => state.open);
  const setOpen = useStore(datapilotStore, (state) => state.setOpen);
  const floatingOffset = useStore(datapilotStore, (state) => state.floatingOffset);
  const setFloatingOffset = useStore(datapilotStore, (state) => state.setFloatingOffset);
  const dragRef = useRef<FloatingButtonDragState | null>(null);
  const suppressClickRef = useRef(false);

  const handlePointerDown = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: floatingOffset.x,
      originY: floatingOffset.y,
      width: rect.width || DEFAULT_FLOATING_BUTTON_SIZE.width,
      height: rect.height || DEFAULT_FLOATING_BUTTON_SIZE.height,
    };
    suppressClickRef.current = false;
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }, [floatingOffset.x, floatingOffset.y]);

  const handleClick = () => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }

    setOpen(true);
  };

  useEffect(() => {
    if (open) {
      dragRef.current = null;
      return undefined;
    }

    const handlePointerMove = (event: globalThis.PointerEvent) => {
      const drag = dragRef.current;
      if (!drag || event.pointerId !== drag.pointerId) {
        return;
      }

      const nextX = drag.originX + event.clientX - drag.startX;
      const nextY = drag.originY + event.clientY - drag.startY;
      if (Math.abs(nextX - drag.originX) + Math.abs(nextY - drag.originY) > 4) {
        suppressClickRef.current = true;
      }

      setFloatingOffset(
        visibleFloatingOffset(
          { x: nextX, y: nextY },
          currentViewport(),
          { width: drag.width, height: drag.height },
        ),
      );
    };

    const handlePointerUp = (event: globalThis.PointerEvent) => {
      const drag = dragRef.current;
      if (!drag || event.pointerId !== drag.pointerId) {
        return;
      }

      dragRef.current = null;
    };

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    window.addEventListener("pointercancel", handlePointerUp);

    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerUp);
    };
  }, [open, setFloatingOffset]);

  if (open) {
    return null;
  }

  return (
    <button
      type="button"
      aria-label="Open DataPilot"
      onPointerDown={handlePointerDown}
      onClick={handleClick}
      className="fixed bottom-5 right-5 z-[80] flex h-14 touch-none cursor-grab items-center gap-3 rounded-full border border-console-line bg-console-text px-4 text-white shadow-[0_18px_42px_rgba(23,32,46,0.18)] transition-[background-color,box-shadow,filter] duration-200 hover:bg-slate-800 hover:shadow-[0_22px_46px_rgba(23,32,46,0.22)] active:cursor-grabbing active:brightness-95 focus:outline-none focus:ring-2 focus:ring-console-cyan focus:ring-offset-2 focus:ring-offset-console-bg"
      style={{
        transform: `translate3d(${floatingOffset.x}px, ${floatingOffset.y}px, 0)`,
      }}
    >
      <span className="flex h-9 w-9 items-center justify-center rounded-full bg-white/12" aria-hidden="true">
        <Bot className="h-5 w-5" />
      </span>
      <span className="hidden text-left sm:block">
        <span className="block text-sm font-semibold leading-4">DataPilot</span>
        <span className="flex items-center gap-1 text-[11px] leading-4 text-slate-300">
          <MessageSquareText className="h-3 w-3" aria-hidden="true" />
          智能体助手
        </span>
      </span>
    </button>
  );
}
