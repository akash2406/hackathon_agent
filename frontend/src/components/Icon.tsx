/** Minimal stroke icons (24px grid, currentColor) so the bundle needs no icon library. */
const PATHS: Record<string, string> = {
  overview: "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z",
  spend: "M4 19V9m6 10V5m6 14v-7m4 7H2",
  trends: "M3 17l6-6 4 4 8-8M15 7h6v6",
  savings: "M12 3v18M16.5 7.5c0-1.9-2-3-4.5-3s-4.5 1.1-4.5 3 2 2.6 4.5 3 4.5 1.1 4.5 3-2 3-4.5 3-4.5-1.1-4.5-3",
  inventory: "M12 2l9 5-9 5-9-5 9-5zm-9 10l9 5 9-5M3 17l9 5 9-5",
  ask: "M21 12a8 8 0 01-11.6 7.1L4 20l1-4.6A8 8 0 1121 12z",
  shield: "M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6l8-3zm-3.5 9l2.5 2.5 4.5-5",
  refresh: "M20 11a8 8 0 10-2.3 5.7M20 4v7h-7",
  spark: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z",
  external: "M14 4h6v6M10 14L20 4M19 13v6a1 1 0 01-1 1H5a1 1 0 01-1-1V6a1 1 0 011-1h6",
  logout: "M15 17l5-5-5-5M20 12H9M12 21H5a1 1 0 01-1-1V4a1 1 0 011-1h7",
  alert: "M12 8v5m0 3.5v.5M10.3 3.9L2.4 18a2 2 0 001.7 3h15.8a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z",
};

export function Icon({ name, size = 18 }: { name: keyof typeof PATHS | string; size?: number }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name] ?? PATHS.spark} />
    </svg>
  );
}
