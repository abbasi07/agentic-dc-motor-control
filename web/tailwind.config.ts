import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Midnight Void → Deep Slate engineering console
        // Use <alpha-value> so utilities like text-cloud/90 and bg-ink-850/80 work.
        ink: {
          950: "rgb(11 17 32 / <alpha-value>)",
          900: "rgb(17 24 39 / <alpha-value>)",
          850: "rgb(30 41 59 / <alpha-value>)",
          800: "rgb(36 52 71 / <alpha-value>)",
          700: "rgb(51 65 85 / <alpha-value>)",
          600: "rgb(71 85 105 / <alpha-value>)",
        },
        cloud: "rgb(248 250 252 / <alpha-value>)",
        accent: {
          DEFAULT: "rgb(16 185 129 / <alpha-value>)",
          soft: "rgb(5 150 105 / <alpha-value>)",
        },
        violet: {
          DEFAULT: "rgb(139 92 246 / <alpha-value>)",
          soft: "rgb(124 58 237 / <alpha-value>)",
        },
        ok: "rgb(16 185 129 / <alpha-value>)",
        warn: "rgb(245 158 11 / <alpha-value>)",
        danger: "rgb(244 63 94 / <alpha-value>)",
      },
      fontFamily: {
        sans: [
          "var(--font-sans)",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
        mono: [
          "var(--font-mono)",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      boxShadow: {
        panel: "0 1px 0 0 rgb(255 255 255 / 0.04) inset",
        glow: "0 0 0 1px rgb(16 185 129 / 0.25), 0 8px 24px -8px rgb(16 185 129 / 0.35)",
      },
    },
  },
  plugins: [],
};

export default config;
