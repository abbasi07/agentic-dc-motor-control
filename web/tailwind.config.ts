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
        // Dark "engineering console" palette — the copilot IS the app.
        ink: {
          950: "#0a0e14",
          900: "#0f141c",
          850: "#141b26",
          800: "#1a2332",
          700: "#243044",
          600: "#33425c",
        },
        accent: {
          DEFAULT: "#4f9cf9",
          soft: "#2b6cb0",
        },
        ok: "#3fb950",
        warn: "#d29922",
        danger: "#f85149",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
