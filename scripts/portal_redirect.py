#!/usr/bin/env python3
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

TARGET = os.environ.get("FILAWIZARD_TARGET", "http://filawizard.local:5000")

CAPTIVE_PATHS = {
    "/generate_204",
    "/gen_204",
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/ncsi.txt",
    "/connecttest.txt",
    "/success.txt",
    "/",
}

class Handler(BaseHTTPRequestHandler):
    def _send_redirect(self):
        self.send_response(302)
        self.send_header("Location", TARGET)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_ok(self):
        body = f"""<html><head>
<meta http-equiv="refresh" content="0; url={TARGET}">
</head><body>Redirecting to <a href="{TARGET}">{TARGET}</a>...</body></html>"""
        body_b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body_b)

    def do_GET(self):
        if self.path in CAPTIVE_PATHS or self.path.startswith("/generate_204"):
            self._send_ok()
        else:
            self._send_redirect()

    def do_HEAD(self):
        self._send_redirect()

    def log_message(self, fmt, *args):
        return

def main():
    host = "0.0.0.0"
    port = int(os.environ.get("FILAWIZARD_PORT", "80"))
    httpd = HTTPServer((host, port), Handler)
    print(f"FilaWizard captive portal redirect running on {host}:{port} -> {TARGET}")
    httpd.serve_forever()

if __name__ == "__main__":
    main()
