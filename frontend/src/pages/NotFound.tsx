import { Link } from "react-router-dom";
import { Compass } from "lucide-react";

export default function NotFound() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center animate-fade-up">
      <span className="mb-4 flex h-14 w-14 items-center justify-center rounded-full border border-edge-strong bg-surface-2 text-ink-faint">
        <Compass size={22} />
      </span>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">Page not found</h1>
      <p className="mt-2 text-sm text-ink-faint">This route doesn't exist — or it moved.</p>
      <Link
        to="/dashboard"
        className="mt-5 inline-flex h-9 items-center rounded-field bg-accent-600 px-4 text-sm font-medium text-white transition-colors hover:bg-accent-500"
      >
        Back to the dashboard
      </Link>
    </div>
  );
}
