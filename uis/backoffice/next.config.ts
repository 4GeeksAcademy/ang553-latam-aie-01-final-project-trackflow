import path from "node:path";
import type { NextConfig } from "next";

/** Backend URL used for server-side rewrites (Docker DNS). */
const backendUrl: string =
  process.env.API_BACKEND_URL ?? "http://api:8000";

const nextConfig: NextConfig = {
  turbopack: {
    root: path.resolve(__dirname, "../.."),
  },
  experimental: {
    turbopackFileSystemCacheForDev: false,
  },
  async rewrites() {
    return [
      {
        source: "/auth/:path*",
        destination: `${backendUrl}/auth/:path*`,
      },
      {
        source: "/users/:path*",
        destination: `${backendUrl}/users/:path*`,
      },
      {
        source: "/profiles/:path*",
        destination: `${backendUrl}/profiles/:path*`,
      },
      {
        source: "/inventory/:path*",
        destination: `${backendUrl}/inventory/:path*`,
      },
      {
        source: "/api/incidents/:path*",
        destination: `${backendUrl}/api/incidents/:path*`,
      },
      {
        source: "/api/suppliers/:path*",
        destination: `${backendUrl}/api/suppliers/:path*`,
      },
    ];
  },
};

export default nextConfig;
