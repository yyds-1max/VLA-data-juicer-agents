import type { LucideIcon } from "lucide-react";

export type ConsolePageId = "dashboard" | "agent" | "data" | "annotate" | "model" | "simulation";

export type Accent = "accent" | "accent2" | "warn" | "danger" | "purple" | "muted";

export type NavItem = {
  id: ConsolePageId;
  label: string;
  group: string;
  icon: LucideIcon;
};

export type TabItem<T extends string> = {
  id: T;
  label: string;
};

export type StatusTone = "success" | "info" | "warning" | "danger" | "neutral" | "purple";
