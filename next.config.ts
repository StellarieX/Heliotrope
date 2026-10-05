import type { NextConfig } from "next";

/**
 * Backend routing strategy (Vercel + Docker end-to-end):
 *
 * - The browser bundle calls `${NEXT_PUBLIC_BACKEND_URL}/api/v1/...`
 *   (see lib/api/client.ts, default http://localhost:8000).
 * - In Vercel production, leave NEXT_PUBLIC_BACKEND_URL unset (Vercel cannot store
 *   an empty value; unset means same-origin in a production build) so
 *   the bundle uses same-origin relative URLs (`/api/v1/...`), and the
 *   rewrite below proxies them server-side to the real backend. Same-origin
 *   calls need no CORS and expose no backend host to the browser.
 * - The rewrite target is BACKEND_URL (server-only env, never NEXT_PUBLIC_*).
 *   Set it in the Vercel dashboard to the deployed FastAPI URL
 *   (e.g. https://heliotrope-api.example.com). Falls back to
 *   NEXT_PUBLIC_BACKEND_URL, then localhost:8000 for `next dev`.
 */
const RAW_BACKEND_URL =
  process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL || "";
const BACKEND_URL = RAW_BACKEND_URL.replace(/\/+$/, "");
const HAS_BACKEND = BACKEND_URL.length > 0;

const nextConfig: NextConfig = {
  async rewrites() {
    // Same-origin mode (NEXT_PUBLIC_BACKEND_URL="" + BACKEND_URL set):
    // browser calls /api/v1/*, Next proxies server-side to the real backend.
    // Direct mode (local dev, NEXT_PUBLIC_BACKEND_URL=http://localhost:8000):
    // bundle calls backend directly; no rewrite needed. Skipping the rewrite
    // when BACKEND_URL is unset avoids proxying to localhost in production.
    if (!HAS_BACKEND) return [];
    return [
      {
        source: "/api/v1/:path*",
        destination: `${BACKEND_URL}/api/v1/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
