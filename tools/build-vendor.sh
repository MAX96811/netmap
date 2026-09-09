#!/bin/sh
# Build the vendored Excalidraw bundle into web/vendor (~22 MB).
#
# The bundle is deliberately not committed: it is reproducible build output.
# The map works fully without it — only the Draw (annotation) layer needs it.
#
# Needs node + npm. Everything happens in a throwaway directory.
set -e
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

npm init -y >/dev/null
npm install --no-audit --no-fund @excalidraw/excalidraw@0.18.1 react@18 react-dom@18 esbuild >/dev/null

# The whole "source": hand the three globals web/app.js expects to the window.
cat > entry.jsx <<'JS'
import React from "react";
import { createRoot } from "react-dom/client";
import * as ExcalidrawLib from "@excalidraw/excalidraw";
window.React = React;
window.ExcalidrawCreateRoot = createRoot;
window.ExcalidrawLib = ExcalidrawLib;
JS

# --conditions=production is required or the CSS export does not resolve.
# --splitting keeps the entry at ~0.7 MB; iife would inline all 178 locale
# chunks into one 8 MB file.
npx esbuild entry.jsx --bundle --minify --format=esm --splitting --outdir=out \
    --conditions=production '--define:process.env.NODE_ENV="production"' \
    --loader:.woff2=file --loader:.ttf=file --loader:.png=file --loader:.svg=file

rm -rf "$HERE/web/vendor"
mkdir -p "$HERE/web/vendor"
cp -r out/. "$HERE/web/vendor/"
mv "$HERE/web/vendor/entry.jsx.js" "$HERE/web/vendor/entry.js" 2>/dev/null || true
mv "$HERE/web/vendor/entry.jsx.css" "$HERE/web/vendor/entry.css" 2>/dev/null || true
# Excalidraw resolves fonts at runtime against EXCALIDRAW_ASSET_PATH.
cp -r node_modules/@excalidraw/excalidraw/dist/prod/fonts "$HERE/web/vendor/fonts"

cat > "$HERE/web/vendor/README.txt" <<TXT
Excalidraw 0.18.1, bundled locally so the map has no external dependencies.
Build output of tools/build-vendor.sh — not committed, safe to delete.
TXT
echo "built web/vendor ($(du -sh "$HERE/web/vendor" | cut -f1))"
