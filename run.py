"""Starts the shared backend and both portals.

    Backend API      http://localhost:5000
    Employee portal  http://localhost:5001
    HR portal        http://localhost:5002
"""
import logging
import os
import sys
import threading
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from flask import Flask, send_from_directory  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

import app as backend  # noqa: E402

HOST = os.environ.get("HOST", "127.0.0.1")
API_PORT, EMPLOYEE_PORT, HR_PORT = 5000, 5001, 5002


def portal(folder):
    """A tiny static-file site for one portal, plus the shared assets."""
    site = Flask(__name__, static_folder=None)
    site_dir = os.path.join(ROOT, folder)
    shared_dir = os.path.join(ROOT, "shared")

    @site.get("/")
    def index():
        return send_from_directory(site_dir, "index.html", max_age=0)

    @site.get("/shared/<path:name>")
    def shared(name):
        return send_from_directory(shared_dir, name, max_age=0)

    @site.get("/<path:name>")
    def files(name):
        return send_from_directory(site_dir, name, max_age=0)

    return site


def serve(name, wsgi_app, port):
    try:
        server = make_server(HOST, port, wsgi_app, threaded=True)
    except OSError:
        print(f"\n  ERROR: port {port} is already in use, so the {name} can't start.")
        print("  Close the other window running this app (or whatever uses the port) and try again.\n")
        os._exit(1)
    threading.Thread(target=server.serve_forever, name=name, daemon=True).start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    serve("backend API", backend.app, API_PORT)
    serve("Employee portal", portal("employee_portal"), EMPLOYEE_PORT)
    serve("HR portal", portal("hr_portal"), HR_PORT)

    caps = backend.extractor.capabilities()
    if caps["ai"]:
        reader = f"Claude AI ({caps['model']})" + (", local OCR as fallback" if caps["ocr"] else "")
    elif caps["ocr"]:
        reader = "local OCR (Tesseract). Add ANTHROPIC_API_KEY to backend\\.env to use Claude AI"
    else:
        reader = "NONE. Add ANTHROPIC_API_KEY to backend\\.env or install Tesseract; until then fields are typed by hand"

    print(f"""
  Fuel Reimbursement System is running
  ------------------------------------
  Employee portal : http://localhost:{EMPLOYEE_PORT}   (amit / emp123, priya / emp123, rahul / emp123)
  HR portal       : http://localhost:{HR_PORT}   (hr / hr123)
  Backend API     : http://localhost:{API_PORT}
  Bill reader     : {reader}

  Press Ctrl+C to stop.
""")
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://localhost:{EMPLOYEE_PORT}")
        webbrowser.open(f"http://localhost:{HR_PORT}")

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
