/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Servidor autónomo (.next/standalone/server.js) para la imagen Docker (DWH_README.md §23).
  // Las variables DWH_* son solo de servidor y se leen en tiempo de ejecución (no se incrustan).
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
