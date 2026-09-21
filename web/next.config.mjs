/** @type {import('next').NextConfig} */
export default {
  poweredByHeader: false,
  distDir: process.env.NEXT_DIST_DIR || ".next",     // lets a second (test) dev server run without clobbering the first
  experimental: { serverComponentsExternalPackages: ["postgres"] },
  // The app now has five pages. Old links keep working.
  async redirects() {
    return [
      { source: "/events", destination: "/news", permanent: false },
      { source: "/trade", destination: "/trades", permanent: false },
      { source: "/predictions", destination: "/signals?tab=predictions", permanent: false },
      { source: "/performance", destination: "/signals?tab=accuracy", permanent: false },
      { source: "/learning", destination: "/signals?tab=learning", permanent: false },
      { source: "/health", destination: "/settings?tab=health", permanent: false },
      { source: "/:coin(btc|eth|xrp)", destination: "/", permanent: false },
    ];
  },
};
