export function Logo({ large }: { large?: boolean }) {
  return (
    <span className={`inline-flex items-center gap-2 ${large ? "text-lg" : "text-sm"}`}>
      <span
        className={
          "flex items-center justify-center rounded-lg bg-gradient-to-br from-accent-400 to-accent-700 font-bold text-white " +
          (large ? "h-8 w-8 text-base" : "h-6 w-6 text-xs")
        }
      >
        C
      </span>
      <span className="font-semibold tracking-tight text-ink">
        Credit<span className="text-accent-400">Flow</span>
      </span>
    </span>
  );
}
