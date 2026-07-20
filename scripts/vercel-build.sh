#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/frontend"
npm run build

mkdir -p "$ROOT/public"
rm -rf "$ROOT/public/"*
cp -R "$ROOT/frontend/dist/." "$ROOT/public/"

echo "Frontend copied to public/ for Vercel CDN."
