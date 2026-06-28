import type * as React from "react";

import { cn } from "../../lib/utils";

type ConsoleButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "tab";
};

function buttonClassForVariant(variant: NonNullable<ConsoleButtonProps["variant"]>) {
  const base =
    "inline-flex h-9 items-center justify-center gap-2 rounded border px-3 text-sm font-medium transition focus:outline-none focus:ring-2 focus:ring-console-cyan disabled:cursor-not-allowed disabled:opacity-50";

  if (variant === "primary") {
    return cn(base, "border-console-cyan/45 bg-console-cyan text-console-bg shadow-[0_10px_24px_rgba(21,209,216,0.22)] hover:bg-cyan-200");
  }

  if (variant === "tab") {
    return cn(base, "border-console-line bg-console-panel2 text-console-muted hover:border-console-cyan/40 hover:text-console-cyan");
  }

  return cn(base, "border-console-line bg-console-panel2 text-console-text hover:border-console-cyan/40 hover:bg-console-cyan/10 hover:text-console-cyan");
}

export function ConsoleButton({ variant = "ghost", className, ...props }: ConsoleButtonProps) {
  return <button type="button" className={cn(buttonClassForVariant(variant), className)} {...props} />;
}
