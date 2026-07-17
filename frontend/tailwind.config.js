/**
 * CreditFlow design tokens.
 *
 * Every color is an RGB-channel CSS variable defined in src/theme/tokens.css,
 * mapped here so Tailwind opacity modifiers (e.g. bg-accent/10) keep working.
 * Components must reference these token names — never raw hex values.
 */

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "rgb(var(--c-bg) / <alpha-value>)",
        surface: {
          DEFAULT: "rgb(var(--c-surface) / <alpha-value>)",
          2: "rgb(var(--c-surface-2) / <alpha-value>)",
          3: "rgb(var(--c-surface-3) / <alpha-value>)",
        },
        edge: {
          DEFAULT: "rgb(var(--c-edge) / <alpha-value>)",
          strong: "rgb(var(--c-edge-strong) / <alpha-value>)",
        },
        ink: {
          DEFAULT: "rgb(var(--c-ink) / <alpha-value>)",
          soft: "rgb(var(--c-ink-soft) / <alpha-value>)",
          faint: "rgb(var(--c-ink-faint) / <alpha-value>)",
        },
        accent: {
          300: "rgb(var(--c-accent-300) / <alpha-value>)",
          400: "rgb(var(--c-accent-400) / <alpha-value>)",
          DEFAULT: "rgb(var(--c-accent-500) / <alpha-value>)",
          500: "rgb(var(--c-accent-500) / <alpha-value>)",
          600: "rgb(var(--c-accent-600) / <alpha-value>)",
          700: "rgb(var(--c-accent-700) / <alpha-value>)",
        },
        success: "rgb(var(--c-success) / <alpha-value>)",
        warning: "rgb(var(--c-warning) / <alpha-value>)",
        danger: "rgb(var(--c-danger) / <alpha-value>)",
        info: "rgb(var(--c-info) / <alpha-value>)",
        // Chart series — validated (dataviz six-checks) against the surface.
        series: {
          1: "rgb(var(--c-series-1) / <alpha-value>)",
          2: "rgb(var(--c-series-2) / <alpha-value>)",
          3: "rgb(var(--c-series-3) / <alpha-value>)",
          4: "rgb(var(--c-series-4) / <alpha-value>)",
        },
      },
      fontFamily: {
        sans: [
          "Inter Variable",
          "Inter",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Cascadia Code",
          "Consolas",
          "monospace",
        ],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      borderRadius: {
        card: "var(--radius-card)",
        field: "var(--radius-field)",
      },
      boxShadow: {
        card: "0 1px 2px 0 rgb(0 0 0 / 0.35), 0 8px 24px -12px rgb(0 0 0 / 0.5)",
        pop: "0 4px 8px -2px rgb(0 0 0 / 0.5), 0 16px 48px -12px rgb(0 0 0 / 0.7)",
        "glow-accent": "0 0 0 1px rgb(var(--c-accent-500) / 0.35), 0 0 24px -4px rgb(var(--c-accent-500) / 0.4)",
      },
      keyframes: {
        "fade-up": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "scale-in": {
          from: { opacity: "0", transform: "scale(0.97)" },
          to: { opacity: "1", transform: "scale(1)" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
        blink: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.35s cubic-bezier(0.21, 1.02, 0.73, 1) both",
        "fade-in": "fade-in 0.2s ease-out both",
        "scale-in": "scale-in 0.18s cubic-bezier(0.21, 1.02, 0.73, 1) both",
        shimmer: "shimmer 1.6s infinite",
        blink: "blink 1s step-end infinite",
      },
    },
  },
  plugins: [],
};
