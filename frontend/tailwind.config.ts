import type { Config } from "tailwindcss";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        console: {
          bg: "#f5f7fb",
          panel: "#ffffff",
          panel2: "#f8fafc",
          line: "#d9e1eb",
          text: "#17202e",
          muted: "#637083",
          cyan: "#2d6cdf",
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
