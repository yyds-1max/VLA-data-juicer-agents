import type { Config } from "tailwindcss";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        console: {
          bg: "#08111f",
          panel: "#0d1829",
          panel2: "#101f35",
          line: "#1e3a5f",
          text: "#eaf2ff",
          muted: "#8ca2c4",
          cyan: "#15d1d8",
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
