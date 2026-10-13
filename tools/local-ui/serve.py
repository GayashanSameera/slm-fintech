"""Serve the local test UI on 127.0.0.1 only. Standard library, no dependencies.

Usage: python3 tools/local-ui/serve.py [port]     (default port 8090)

Why a server at all? Browsers refuse to load JavaScript modules (app.js, pdf.js) from
file:// URLs, so the page must come from http://localhost. This file is a minimal static
file server for that purpose.

Why 127.0.0.1? Binding to the loopback address means only this machine can open the page.
Other computers on the network cannot reach it, which matters because real documents are
handled here.
"""

import functools
import http.server
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent


class Handler(http.server.SimpleHTTPRequestHandler):
    # ES modules must be served as JavaScript or the browser refuses them.
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
    }

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:  # keep the terminal quiet
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    if not (ROOT / "vendor" / "pdf.min.mjs").exists():
        sys.exit("Libraries missing. Run tools/local-ui/fetch-vendor.sh first.")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=str(ROOT)))
    print(f"SLM Extraction Bench: http://localhost:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
