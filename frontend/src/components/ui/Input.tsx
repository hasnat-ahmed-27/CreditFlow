import {
  forwardRef,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

const FIELD_CLASSES =
  "w-full rounded-field border border-edge-strong bg-surface-2/60 px-3 text-sm text-ink " +
  "placeholder:text-ink-faint transition-colors duration-150 " +
  "hover:border-edge-strong focus:border-accent-500/70 focus:bg-surface-2 " +
  "focus:outline-none focus:ring-2 focus:ring-accent-500/25 " +
  "disabled:cursor-not-allowed disabled:opacity-55";

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label?: string;
  hint?: string;
  error?: string | null;
  children: ReactNode;
}) {
  return (
    <label className="block">
      {label && (
        <span className="mb-1.5 block text-xs font-medium text-ink-soft">{label}</span>
      )}
      {children}
      {error ? (
        <span className="mt-1 block text-xs text-danger">{error}</span>
      ) : hint ? (
        <span className="mt-1 block text-xs text-ink-faint">{hint}</span>
      ) : null}
    </label>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className = "", ...rest }, ref) {
    return <input ref={ref} className={`${FIELD_CLASSES} h-9 ${className}`} {...rest} />;
  },
);

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className = "", ...rest }, ref) {
  return (
    <textarea
      ref={ref}
      className={`${FIELD_CLASSES} min-h-[5rem] resize-y py-2 leading-relaxed ${className}`}
      {...rest}
    />
  );
});

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className = "", children, ...rest }, ref) {
    return (
      <select ref={ref} className={`${FIELD_CLASSES} h-9 appearance-none pr-8 ${className}`} {...rest}>
        {children}
      </select>
    );
  },
);
