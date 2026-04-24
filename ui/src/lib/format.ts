export function truncateText(input: string, max: number): string {
  if (max <= 1) return "…";
  return input.length > max ? input.slice(0, max - 1) + "…" : input;
}

export function truncateSmart(input: string, max: number): string {
  if (max < 8) return "…";
  return input.length <= max ? input : input.slice(0, Math.max(0, max - 1)) + "…";
}

export function formatTokenCount(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}
