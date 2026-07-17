import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Logo } from "../../components/layout/Logo";

export function AuthLayout({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-bg px-4 py-10">
      {/* ambient gradient wash */}
      <div className="pointer-events-none absolute -top-48 left-1/2 h-96 w-[42rem] -translate-x-1/2 rounded-full bg-accent-600/[0.13] blur-3xl" />
      <div className="pointer-events-none absolute bottom-0 right-0 h-72 w-72 rounded-full bg-info/[0.06] blur-3xl" />

      <div className="relative w-full max-w-sm animate-fade-up">
        <div className="mb-7 flex justify-center">
          <Link to="/">
            <Logo large />
          </Link>
        </div>
        <div className="rounded-card border border-edge bg-surface/90 p-6 shadow-pop backdrop-blur">
          <h1 className="text-lg font-semibold tracking-tight text-ink">{title}</h1>
          {subtitle && <p className="mt-1 text-xs leading-relaxed text-ink-faint">{subtitle}</p>}
          <div className="mt-5">{children}</div>
        </div>
        {footer && <div className="mt-4 text-center text-xs text-ink-faint">{footer}</div>}
      </div>
    </div>
  );
}
