import type * as React from "react";

import { cn } from "../../lib/utils";

export function ConsoleCard({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("rounded-lg border border-console-line bg-console-panel p-4 shadow-sm", className)}>
      {children}
    </section>
  );
}
