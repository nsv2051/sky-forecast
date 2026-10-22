#!/usr/bin/env python3
"""Zero-dependency local preview server for the sky-forecast static site.

Serves the ``public/`` directory (including ``public/data/forecast.json``) over
plain HTTP using only the Python standard library::

    python3 scripts/serve.py            # http://127.0.0.1:8080/
    python3 scripts/serve.py --port 9000
    python3 scripts/serve.py --no-browser

This is a convenience for local preview only; production hosting is handled by
Vercel (see ``vercel.json``).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import os
import socket
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = PROJECT_ROOT / "public"
DEFAULT_PORT = 8080


class PreviewHandler(http.server.SimpleHTTPRequestHandler):
    """Static handler with dev-friendly caching and quiet logging."""

    def end_headers(self) -> None:
        # Never cache during local preview so edits show up instantly.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, fmt, *args):  # noqa: D102 - concise one-liner log
        sys.stderr.write("[serve] %s - %s\n" % (self.address_string(), fmt % args))


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview the sky-forecast site locally.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: %(default)s)")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)),
        help="TCP port (default: %(default)s)",
    )
    parser.add_argument(
        "--dir", default=str(DEFAULT_DIR), help="directory to serve (default: public/)",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.dir).resolve()
    if not root.is_dir():
        print("error: directory not found: %s" % root, file=sys.stderr)
        return 2

    handler = functools.partial(PreviewHandler, directory=str(root))
    with contextlib.suppress(OSError):
        os.chdir(root)

    try:
        httpd = ReusableTCPServer((args.host, args.port), handler)
    except OSError as exc:
        print("error: cannot bind %s:%d (%s)" % (args.host, args.port, exc), file=sys.stderr)
        return 1

    display_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    url = "http://%s:%d/" % (display_host, args.port)
    print("sky-forecast preview")
    print("  serving : %s" % root)
    print("  local   : %s" % url)
    if args.host in {"0.0.0.0", "::"}:
        print("  network : http://%s:%d/" % (_local_ip(), args.port))
    print("  data    : %sdata/forecast.json" % url)
    print("press Ctrl+C to stop")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
