/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The API is a separate FastAPI service (browser talks to it directly via CORS).
  // Enable standalone output so a production image stays small if we containerize later.
  output: "standalone",
};

export default nextConfig;
