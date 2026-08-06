"use client"

import * as React from "react"
import { Progress as ProgressPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

type AccessibleName =
  | { "aria-label": string; "aria-labelledby"?: never }
  | { "aria-label"?: never; "aria-labelledby": string }

export type CircularProgressProps = Omit<
  React.ComponentProps<typeof ProgressPrimitive.Root>,
  "value" | "max" | "children"
> &
  AccessibleName & {
    value?: number | null
    centerLabel?: React.ReactNode
    showValue?: boolean
    trackClassName?: string
    indicatorClassName?: string
  }

function normalizeProgressValue(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return 0
  }

  return Math.min(100, Math.max(0, value))
}

function CircularProgress({
  className,
  value,
  centerLabel,
  showValue = true,
  trackClassName,
  indicatorClassName,
  ...props
}: CircularProgressProps) {
  const normalizedValue = normalizeProgressValue(value)

  return (
    <ProgressPrimitive.Root
      data-slot="circular-progress"
      value={normalizedValue}
      max={100}
      className={cn(
        "relative inline-grid size-24 shrink-0 place-items-center text-primary",
        className
      )}
      {...props}
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 100 100"
        className="absolute inset-0 size-full overflow-visible"
      >
        <circle
          cx="50"
          cy="50"
          r="44"
          fill="none"
          stroke="currentColor"
          strokeWidth="8"
          className={cn("text-muted", trackClassName)}
        />
        <circle
          data-slot="circular-progress-indicator"
          cx="50"
          cy="50"
          r="44"
          pathLength="100"
          fill="none"
          stroke="currentColor"
          strokeWidth="8"
          strokeLinecap="round"
          className={cn(
            "origin-center -rotate-90 transition-[stroke-dashoffset] duration-200 ease-out motion-reduce:transition-none",
            indicatorClassName
          )}
          style={{
            strokeDasharray: 100,
            strokeDashoffset: 100 - normalizedValue,
          }}
        />
      </svg>

      {showValue ? (
        <span
          aria-hidden="true"
          data-slot="circular-progress-label"
          className="relative z-10 text-center text-base font-semibold tabular-nums text-foreground"
        >
          {centerLabel ?? `${Math.round(normalizedValue)}%`}
        </span>
      ) : null}
    </ProgressPrimitive.Root>
  )
}

export { CircularProgress, normalizeProgressValue }
