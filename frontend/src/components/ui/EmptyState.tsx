import type { ReactNode } from "react";
import { CloudOff } from "lucide-react";
import type { ApiError } from "../../lib/api/client";
import { Button } from "./Button";

export function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon?: ReactNode;
  title: string;
  body?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center animate-fade-in">
      {icon && (
        <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full border border-edge-strong bg-surface-2 text-ink-faint">
          {icon}
        </div>
      )}
      <h3 className="text-sm font-semibold text-ink">{title}</h3>
      {body && <p className="mt-1.5 max-w-sm text-xs leading-relaxed text-ink-faint">{body}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/**
 * Uniform degradation for a failed fetch: distinguishes "backend not
 * reachable / not wired yet" from a real API error, and offers a retry.
 */
export function ErrorState({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  const offline = error.isNetwork || error.status === 502 || error.status === 504;
  return (
    <EmptyState
      icon={<CloudOff size={20} />}
      title={offline ? "Service unavailable" : "Couldn't load this view"}
      body={
        offline
          ? "The backend for this screen isn't reachable right now. It will populate as soon as the service is up."
          : error.message
      }
      action={
        onRetry && (
          <Button variant="secondary" size="sm" onClick={onRetry}>
            Try again
          </Button>
        )
      }
    />
  );
}
