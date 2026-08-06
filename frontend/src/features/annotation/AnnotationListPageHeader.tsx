import type { ReactNode } from "react";

type AnnotationListPageHeaderProps = {
  headingId: string;
  title: string;
  description: string;
  actions: ReactNode;
};

export function AnnotationListPageHeader({
  headingId,
  title,
  description,
  actions,
}: AnnotationListPageHeaderProps) {
  return (
    <header className="flex min-h-14 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <h2
          id={headingId}
          className="text-xl font-semibold tracking-tight text-slate-950"
        >
          {title}
        </h2>
        <p className="mt-1.5 text-sm text-slate-500">{description}</p>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>
    </header>
  );
}
