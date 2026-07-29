import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 演示工程：host:true 便于在容器/远程环境通过 IP 访问
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
  },
  preview: {
    host: true,
    port: 4173,
  },
});
