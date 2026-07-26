import { Link } from "react-router-dom";
import { CheckCircle2 } from "lucide-react";

/**
 * Stripe Checkout redirects here (success_url = /billing/success?session_id=...)
 * after a completed subscription. The plan activation and credit grant happen
 * asynchronously via the webhook, so this page just confirms the payment and
 * points the user at the pages that now reflect it.
 */
export default function BillingSuccess() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center animate-fade-up">
      <span className="mb-4 flex h-14 w-14 items-center justify-center rounded-full border border-edge-strong bg-surface-2 text-success">
        <CheckCircle2 size={24} />
      </span>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">Payment successful</h1>
      <p className="mt-2 max-w-sm text-sm text-ink-faint">
        Your subscription is active. Plan changes and credits are applied automatically —
        they may take a moment to appear.
      </p>
      <div className="mt-5 flex items-center gap-3">
        <Link
          to="/billing"
          className="inline-flex h-9 items-center rounded-field bg-accent-600 px-4 text-sm font-medium text-white transition-colors hover:bg-accent-500"
        >
          Back to billing
        </Link>
        <Link
          to="/credits"
          className="inline-flex h-9 items-center rounded-field border border-edge-strong px-4 text-sm font-medium text-ink transition-colors hover:bg-surface-2"
        >
          View credits
        </Link>
      </div>
    </div>
  );
}
