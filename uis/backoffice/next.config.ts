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
    // A RAG query performs one bounded embedding request and, when context is
    // available, one bounded generation request. Next's 30 s proxy default can
    // therefore close a healthy request before FastAPI returns.
    proxyTimeout: 90_000,
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
        source: "/api/access/:path*",
        destination: `${backendUrl}/api/access/:path*`,
      },
      {
        source: "/api/suppliers/:path*",
        destination: `${backendUrl}/api/suppliers/:path*`,
      },
      {
        source: "/telemetry/:path*",
        destination: `${backendUrl}/telemetry/:path*`,
      },
      {
        source: "/knowledge/:path*",
        destination: `${backendUrl}/knowledge/:path*`,
      },
    ];
  },
};

export default nextConfig;
