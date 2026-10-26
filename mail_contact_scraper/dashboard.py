"""Local live dashboard for output/contacts.csv.

    python -m mail_contact_scraper.dashboard            # just the dashboard
    python -m mail_contact_scraper --dashboard           # scrape + dashboard

Serves on 127.0.0.1 only, so the contact data never leaves this computer.
"""
import argparse
import csv
import json
import logging
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)
DEFAULT_PORT = 8765


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_contacts(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def make_handler(out_dir):
    out_dir = Path(out_dir)
    page = resources.files(__package__).joinpath("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the scraper's console clean
            pass

        def _send(self, body, ctype, status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            csv_path = out_dir / "contacts.csv"
            if url.path == "/":
                self._send(page, "text/html; charset=utf-8")
            elif url.path == "/data":
                since = parse_qs(url.query).get("since", [""])[0]
                version = str(csv_path.stat().st_mtime_ns) if csv_path.exists() else "0"
                payload = {"status": _read_json(out_dir / "status.json"), "version": version}
                if since != version:
                    payload["contacts"] = _read_contacts(csv_path)
                self._send(json.dumps(payload, ensure_ascii=False).encode(), "application/json")
            elif url.path == "/contacts.csv" and csv_path.exists():
                self._send(csv_path.read_bytes(), "text/csv; charset=utf-8")
            else:
                self._send(b"not found", "text/plain", 404)

    return Handler


def serve(out_dir, port=DEFAULT_PORT, open_browser=True):
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), make_handler(out_dir))
            break
        except OSError:
            continue
    else:
        raise OSError(f"No free port between {port} and {port + 19}")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    log.info("Dashboard: %s", url)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    return server, url


def start_in_background(out_dir, port=DEFAULT_PORT):
    server, url = serve(out_dir, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return url


def wait_forever():
    log.info("Dashboard sigue abierto. Ctrl+C para cerrarlo.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="mail_contact_scraper.dashboard")
    p.add_argument("--output", default="output", help="Folder with contacts.csv (default: output)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    server, _ = serve(args.output, args.port, not args.no_browser)
    log.info("Ctrl+C para cerrar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
