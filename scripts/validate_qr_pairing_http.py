"""Exercise the mobile QR protocol over HTTP without exposing QR credentials in output."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path

import httpx
import uvicorn
import zxingcpp
from PIL import Image

from openjarvis.server.app import create_app


class Engine:
    engine_id = "test"

    def health(self) -> bool:
        return True


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="jarivs-http-pair-") as runtime:
        os.environ["OPENJARVIS_HOME"] = str(Path(runtime) / "home")
        port = unused_port()
        base_url = f"http://127.0.0.1:{port}"
        app = create_app(Engine(), "test", engine_name="test", api_key="pairing-http-secret")
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        try:
            with httpx.Client(base_url=base_url, timeout=5.0) as client:
                for _ in range(30):
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Local HTTP test server did not become ready")

                admin_headers = {"Authorization": "Bearer pairing-http-secret"}
                created = client.post("/api/pairing/create", headers=admin_headers, json={"server_url": base_url, "device_name_hint": "Jarivs HTTP validator"})
                created.raise_for_status()
                pairing = created.json()
                if "token" in pairing or "qr_data" in pairing:
                    raise AssertionError("Administrative JSON exposed QR credential data")

                png = client.get(f"/api/pairing/{pairing['pairing_id']}/qr.png", headers=admin_headers)
                png.raise_for_status()
                decoded = zxingcpp.read_barcode(Image.open(BytesIO(png.content)))
                if decoded is None:
                    raise AssertionError("QR PNG could not be decoded")
                qr = json.loads(decoded.text)
                if qr["fingerprint"] != pairing["fingerprint"] or qr["server_url"] != base_url:
                    raise AssertionError("Decoded QR review data does not match administrative metadata")

                consumed = client.post("/api/pairing/consume", json={"pairing_id": qr["pairing_id"], "token": qr["token"], "device_name": "Jarivs HTTP validator"})
                consumed.raise_for_status()
                device = consumed.json()
                device_headers = {"Authorization": f"Bearer {device['device_token']}"}
                resource = client.get("/api/resources/current", headers=device_headers)
                resource.raise_for_status()
                revoked = client.post("/api/pairing/self-revoke", headers=device_headers)
                revoked.raise_for_status()
                denied = client.get("/api/resources/current", headers=device_headers)
                if denied.status_code != 401:
                    raise AssertionError("Revoked device token still accessed Resource API")
                print("PASS: QR PNG decode → fingerprint review → consume → scoped resource request → self-revoke → access denied")
        finally:
            server.should_exit = True
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
