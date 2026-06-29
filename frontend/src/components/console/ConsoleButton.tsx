import type * as React from "react";

import { cn } from "../../lib/utils";

type ConsoleButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "tab";
};

function buttonClassForVariant(variant: NonNullable<ConsoleButtonProps["variant"]>) {
  const base =
    "inline-flex h-9 items-center justify-center gap-2 rounded-lg border px-3 text-sm font-medium transition focus:outline-none focus:ring-2 focus:ring-console-cyan disabled:cursor-not-allowed disabled:opacity-50";

  if (variant === "primary") {
    return cn(base, "border-console-cyan bg-console-cyan text-white shadow-sm hover:bg-blue-700");
  }

  if (variant === "tab") {
    return cn(base, "border-console-line bg-console-panel2 text-console-muted hover:border-console-cyan/40 hover:text-console-text");
  }

  return cn(base, "border-console-line bg-console-panel text-console-text shadow-sm hover:border-console-cyan/40 hover:bg-console-panel2");
}

export function ConsoleButton({ variant = "ghost", className, ...props }: ConsoleButtonProps) {
  return <button type="button" className={cn(buttonClassForVariant(variant), className)} {...props} />;
}
