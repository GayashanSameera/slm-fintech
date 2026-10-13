#!/usr/bin/env bash
# Downloads the two browser libraries the local UI uses and checks them against pinned hashes.
# Run once; afterwards the UI works without internet.
set -euo pipefail
cd "$(dirname "$0")"

PDFJS=4.10.38     # Apache-2.0
MAMMOTH=1.8.0     # BSD-2-Clause

mkdir -p vendor
curl -fsSL -o vendor/pdf.min.mjs         "https://cdn.jsdelivr.net/npm/pdfjs-dist@${PDFJS}/build/pdf.min.mjs"
curl -fsSL -o vendor/pdf.worker.min.mjs  "https://cdn.jsdelivr.net/npm/pdfjs-dist@${PDFJS}/build/pdf.worker.min.mjs"
curl -fsSL -o vendor/mammoth.browser.min.js "https://cdn.jsdelivr.net/npm/mammoth@${MAMMOTH}/mammoth.browser.min.js"

(cd vendor && sha256sum -c ../vendor.sha256)
echo "Libraries ready in $(pwd)/vendor"
