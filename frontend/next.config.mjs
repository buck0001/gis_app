/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://127.0.0.1:8000/api/:path*" },
      { source: "/files/:path*", destination: "http://127.0.0.1:8000/files/:path*" }
    ];
  }
};
export default nextConfig;
