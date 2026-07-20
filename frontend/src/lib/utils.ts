import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Final UI guard; the contract-v1 backend remains the authoritative sanitizer. */
export function withoutPercentages(value: string): string {
  return value.replace(/\s*\d+(?:\.\d+)?\s*[%％]\s*/g, "").replace(/\s{2,}/g, " ").trim();
}
