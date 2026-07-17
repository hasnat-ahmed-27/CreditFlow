import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Loader2 } from "lucide-react";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  icon?: ReactNode;
}

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-accent-600 text-white hover:bg-accent-500 active:bg-accent-700 " +
    "shadow-[inset_0_1px_0_rgb(255_255_255/0.12)] disabled:hover:bg-accent-600",
  secondary:
    "border border-edge-strong bg-surface-2 text-ink hover:bg-surface-3 hover:border-edge-strong " +
    "active:bg-surface-3/80 disabled:hover:bg-surface-2",
  ghost:
    "text-ink-soft hover:text-ink hover:bg-surface-2 active:bg-surface-3 disabled:hover:bg-transparent",
  danger:
    "bg-danger/10 text-danger border border-danger/25 hover:bg-danger/20 active:bg-danger/25 " +
    "disabled:hover:bg-danger/10",
};

const SIZES: Record<Size, string> = {
  sm: "h-8 px-3 text-xs gap-1.5",
  md: "h-9 px-4 text-sm gap-2",
  lg: "h-11 px-5 text-sm gap-2",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size = "md", loading, icon, className = "", children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={
        "inline-flex select-none items-center justify-center whitespace-nowrap rounded-field font-medium " +
        "transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-55 " +
        `${VARIANTS[variant]} ${SIZES[size]} ${className}`
      }
      {...rest}
    >
      {loading ? <Loader2 size={15} className="animate-spin" /> : icon}
      {children}
    </button>
  );
});
