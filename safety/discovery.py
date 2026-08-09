"""ASCOM Alpaca discovery responder.

NINA broadcasts the token 'alpacadiscovery1' to UDP :32227; we reply with our HTTP port
as JSON so it can auto-find the SafetyMonitor. Best-effort: if the socket can't bind,
discovery is skipped and manual entry in NINA still works.
"""
from __future__ import annotations

import json
import logging
import socket
import threading

log = logging.getLogger("ttu.safety.discovery")

DISCOVERY_PORT = 32227
TOKEN = b"alpacadiscovery1"


def start(alpaca_port: int):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
    except OSError as e:
        log.warning("Alpaca discovery disabled (%s); use manual entry in NINA", e)
        return None

    def loop():
        reply = json.dumps({"AlpacaPort": alpaca_port}).encode()
        while True:
            try:
                data, addr = sock.recvfrom(1024)
            except OSError:
                break
            if TOKEN in data:
                try:
                    sock.sendto(reply, addr)
                except OSError:
                    log.exception("discovery reply failed")

    t = threading.Thread(target=loop, name="ttu-safety-discovery", daemon=True)
    t.start()
    log.info("Alpaca discovery responding on udp/%d", DISCOVERY_PORT)
    return t
