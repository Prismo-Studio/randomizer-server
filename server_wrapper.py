"""
Lightweight HTTP wrapper around MultiServer.py for remote hosting.
Accepts seed uploads, starts/stops the Archipelago server, returns status.

Endpoints:
  POST /upload     - Upload a .archipelago file (multipart form)
  POST /start      - Start hosting the latest uploaded seed
  POST /stop       - Stop the running server
  GET  /status     - JSON status (running, seed name, port)
  GET  /seeds      - List available seeds
"""

import json
import os
import signal
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

SEEDS_DIR = Path("/app/seeds")
SEEDS_DIR.mkdir(exist_ok=True)

PORT_WS = int(os.environ.get("AP_PORT", "38281"))
PORT_HTTP = int(os.environ.get("HTTP_PORT", "8080"))

server_process: subprocess.Popen | None = None
server_seed: str | None = None
server_log: list[str] = []
log_lock = threading.Lock()


def _drain_stream(stream, prefix=""):
    """Drain a single stream into server_log."""
    if stream is None:
        return
    for line in iter(stream.readline, ""):
        line = line.strip()
        if line:
            entry = f"{prefix}{line}" if prefix else line
            with log_lock:
                server_log.append(entry)
                if len(server_log) > 200:
                    del server_log[:100]


def start_server(seed_path: str):
    global server_process, server_seed
    stop_server()

    cmd = [
        sys.executable, "MultiServer.py",
        seed_path,
        "--port", str(PORT_WS),
        "--loglevel", "info",
    ]
    env = {**os.environ, "SKIP_REQUIREMENTS_UPDATE": "1", "PYTHONIOENCODING": "utf-8"}
    server_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        cwd="/app",
        env=env,
    )
    server_seed = Path(seed_path).name
    threading.Thread(target=_drain_stream, args=(server_process.stdout,), daemon=True).start()
    threading.Thread(target=_drain_stream, args=(server_process.stderr, "[err] "), daemon=True).start()


def stop_server():
    global server_process, server_seed
    if server_process and server_process.poll() is None:
        server_process.terminate()
        try:
            server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_process.kill()
    server_process = None
    server_seed = None


def get_status():
    running = server_process is not None and server_process.poll() is None
    with log_lock:
        recent = list(server_log[-50:])
    return {
        "running": running,
        "seed": server_seed if running else None,
        "port": PORT_WS,
        "recent_log": recent,
    }


def list_seeds():
    seeds = []
    for f in sorted(SEEDS_DIR.iterdir()):
        if f.suffix == ".archipelago":
            seeds.append({
                "name": f.name,
                "size": f.stat().st_size,
                "path": str(f),
            })
    return seeds


class Handler(BaseHTTPRequestHandler):
    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            self._json(get_status())
        elif self.path == "/seeds":
            self._json(list_seeds())
        elif self.path == "/health":
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/upload":
            self._handle_upload()
        elif self.path == "/start":
            self._handle_start()
        elif self.path == "/stop":
            stop_server()
            self._json(get_status())
        else:
            self._json({"error": "not found"}, 404)

    def _handle_upload(self):
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))

        if "multipart/form-data" in content_type:
            # Simple multipart parsing - read raw body
            body = self.rfile.read(content_length)
            boundary = content_type.split("boundary=")[1].encode()
            parts = body.split(b"--" + boundary)
            for part in parts:
                if b"filename=" in part:
                    # Extract filename
                    header_end = part.find(b"\r\n\r\n")
                    headers = part[:header_end].decode(errors="ignore")
                    file_data = part[header_end + 4:]
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]

                    fname = "uploaded.archipelago"
                    for h in headers.split("\r\n"):
                        if 'filename="' in h:
                            fname = h.split('filename="')[1].split('"')[0]

                    dest = SEEDS_DIR / fname
                    dest.write_bytes(file_data)
                    self._json({"uploaded": fname, "path": str(dest)})
                    return
            self._json({"error": "no file in upload"}, 400)
        else:
            # Raw body upload with filename in query or header
            body = self.rfile.read(content_length)
            fname = self.headers.get("X-Filename", "uploaded.archipelago")
            dest = SEEDS_DIR / fname
            dest.write_bytes(body)
            self._json({"uploaded": fname, "path": str(dest)})

    def _handle_start(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        data = json.loads(body) if body.strip() else {}

        seed_name = data.get("seed")
        if seed_name:
            seed_path = SEEDS_DIR / seed_name
        else:
            # Use most recent seed
            seeds = sorted(SEEDS_DIR.glob("*.archipelago"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not seeds:
                self._json({"error": "no seeds available"}, 400)
                return
            seed_path = seeds[0]

        if not seed_path.exists():
            self._json({"error": f"seed not found: {seed_path.name}"}, 404)
            return

        start_server(str(seed_path))
        self._json(get_status())

    def log_message(self, fmt, *args):
        pass  # Silence request logs


if __name__ == "__main__":
    print(f"HTTP API on :{PORT_HTTP}, MultiServer WS on :{PORT_WS}")
    httpd = HTTPServer(("0.0.0.0", PORT_HTTP), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop_server()
        httpd.shutdown()
