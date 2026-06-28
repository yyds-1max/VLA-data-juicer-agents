import type * as React from "react";

import { cn } from "../../lib/utils";

export function ConsoleCard({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("rounded-xl border border-console-line bg-console-panel p-4 shadow-[0_16px_40px_rgba(0,0,0,0.2)]", className)}>
      {children}
    </section>
  );
}
