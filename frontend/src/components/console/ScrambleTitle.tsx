import * as React from "react";

import { cn } from "../../lib/utils";

const SCRAMBLE_GLYPHS = ["〇", "一", "十", "六", "◇", "◆"] as const;

export type ScrambleTitleElement = "span" | "h1" | "h2" | "h3";

export type ScrambleTitleProps = Omit<
  React.ComponentPropsWithoutRef<"span">,
  "aria-label" | "children"
> & {
  text: string;
  as?: ScrambleTitleElement;
  durationMs?: number;
  stepMs?: number;
};

export function scrambleTitleFrame(text: string, progress: number, frame: number) {
  const characters = Array.from(text);
  const clampedProgress = Math.min(1, Math.max(0, Number.isFinite(progress) ? progress : 1));
  const revealCount = Math.floor(characters.length * clampedProgress);
  const safeFrame = Number.isFinite(frame) ? Math.max(0, Math.floor(frame)) : 0;

  return characters
    .map((character, index) => {
      if (/\s/u.test(character) || index < revealCount || clampedProgress >= 1) {
        return character;
      }
      return SCRAMBLE_GLYPHS[(safeFrame + index) % SCRAMBLE_GLYPHS.length];
    })
    .join("");
}

function readReducedMotionPreference() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function useReducedMotionPreference() {
  const [reducedMotion, setReducedMotion] = React.useState(readReducedMotionPreference);

  React.useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }

    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const handleChange = (event: MediaQueryListEvent) => setReducedMotion(event.matches);
    setReducedMotion(mediaQuery.matches);
    if (typeof mediaQuery.addEventListener === "function") {
      mediaQuery.addEventListener("change", handleChange);
    } else {
      mediaQuery.addListener(handleChange);
    }

    return () => {
      if (typeof mediaQuery.removeEventListener === "function") {
        mediaQuery.removeEventListener("change", handleChange);
      } else {
        mediaQuery.removeListener(handleChange);
      }
    };
  }, []);

  return reducedMotion;
}

export function ScrambleTitle({
  text,
  as = "span",
  durationMs = 380,
  stepMs = 38,
  className,
  ...props
}: ScrambleTitleProps) {
  const reducedMotion = useReducedMotionPreference();
  const [visualState, setVisualState] = React.useState(() => ({
    sourceText: text,
    text: readReducedMotionPreference() ? text : scrambleTitleFrame(text, 0, 0),
    settled: readReducedMotionPreference(),
  }));
  const Component = as;
  const renderedState = visualState.sourceText === text
    ? visualState
    : {
        sourceText: text,
        text: reducedMotion ? text : scrambleTitleFrame(text, 0, 0),
        settled: reducedMotion,
      };

  React.useEffect(() => {
    const hasValidTiming =
      Number.isFinite(durationMs) && durationMs > 0 && Number.isFinite(stepMs) && stepMs > 0;
    if (reducedMotion || !hasValidTiming || text.length === 0) {
      setVisualState({ sourceText: text, text, settled: true });
      return;
    }

    const totalFrames = Math.max(1, Math.ceil(durationMs / stepMs));
    let frame = 0;
    setVisualState({
      sourceText: text,
      text: scrambleTitleFrame(text, 0, frame),
      settled: false,
    });

    const intervalId = window.setInterval(() => {
      frame += 1;
      const progress = Math.min(1, frame / totalFrames);
      setVisualState({
        sourceText: text,
        text: scrambleTitleFrame(text, progress, frame),
        settled: progress >= 1,
      });

      if (progress >= 1) {
        window.clearInterval(intervalId);
      }
    }, stepMs);

    return () => window.clearInterval(intervalId);
  }, [durationMs, reducedMotion, stepMs, text]);

  return (
    <Component
      {...props}
      aria-label={text}
      data-slot="scramble-title"
      data-scramble-state={renderedState.settled ? "settled" : "running"}
      className={cn("inline-block", className)}
    >
      <span
        aria-hidden="true"
        data-slot="scramble-title-value"
        className="relative inline-block overflow-hidden whitespace-pre"
      >
        <span className={cn(!renderedState.settled && "invisible")}>{text}</span>
        {!renderedState.settled ? (
          <span className="absolute inset-0 whitespace-pre">{renderedState.text}</span>
        ) : null}
      </span>
    </Component>
  );
}
