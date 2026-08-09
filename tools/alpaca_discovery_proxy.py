#!/usr/bin/env python3
"""Alpaca discovery proxy — make a remote ASCOM Alpaca device usable by a client (NINA)
that is on a different subnet / behind NAT and can't receive the device's discovery reply.

Run this ON THE NINA PC (or any always-on host on NINA's subnet). It:
  1. answers Alpaca discovery (UDP :32227) so NINA finds "an Alpaca device here", and
  2. reverse-proxies the Alpaca HTTP port straight through to the real device.

NINA then connects to THIS host at :<listen-port>, which is forwarded to <pi>:<pi-port>.
All the real Alpaca traffic rides your existing direct IP connectivity to the Pi.

    python alpaca_discovery_proxy.py --pi 10.0.0.42
    python alpaca_discovery_proxy.py --pi 10.0.0.42 --listen-port 11111 --pi-port 11111

No admin needed (ports are > 1024). Windows may prompt once to allow it through the
firewall — click Allow. Leave the window open while NINA is running.
"""
import argparse
import json
import socket
import sys
import threading

TOKEN = b"alpacadiscovery1"


def log(msg):
    print(msg, flush=True)


def discovery_responder(usock, listen_port, stop):
    reply = json.dumps({"AlpacaPort": listen_port}).encode()
    log(f"[discovery] answering udp/{usock.getsockname()[1]} -> AlpacaPort {listen_port}")
    while not stop.is_set():
        try:
            data, addr = usock.recvfrom(1024)
        except OSError:
            break
        if TOKEN in data:
            try:
                usock.sendto(reply, addr)
                log(f"[discovery] replied to {addr[0]}")
            except OSError:
                pass


def _pump(src, dst):
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client, pi_ip, pi_port):
    try:
        server = socket.create_connection((pi_ip, pi_port), timeout=10)
    except OSError as e:
        log(f"[proxy] cannot reach {pi_ip}:{pi_port} ({e})")
        client.close()
        return
    up = threading.Thread(target=_pump, args=(client, server), daemon=True)
    up.start()
    _pump(server, client)          # runs until the device closes the response
    up.join(timeout=5)
    for s in (client, server):
        try:
            s.close()
        except OSError:
            pass


def tcp_proxy(lsock, pi_ip, pi_port, stop):
    log(f"[proxy] forwarding tcp/{lsock.getsockname()[1]} -> {pi_ip}:{pi_port}")
    while not stop.is_set():
        try:
            client, _ = lsock.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(client, pi_ip, pi_port),
                         daemon=True).start()


def _bind_udp(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    return s


def _bind_tcp(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(32)
    return s


def main():
    ap = argparse.ArgumentParser(description="Alpaca discovery proxy for NINA")
    ap.add_argument("--pi", required=True, help="IP address of the remote Alpaca device")
    ap.add_argument("--pi-port", type=int, default=11111, help="device's Alpaca port")
    ap.add_argument("--listen-port", type=int, default=11111,
                    help="local port NINA will connect to (advertised in discovery)")
    ap.add_argument("--discovery-port", type=int, default=32227)
    args = ap.parse_args()

    try:
        usock = _bind_udp(args.discovery_port)
    except OSError as e:
        log(f"ERROR: can't bind UDP {args.discovery_port} ({e}).")
        log("       Close the OmniSimulator / any other Alpaca app using it, then retry.")
        sys.exit(1)
    try:
        lsock = _bind_tcp(args.listen_port)
    except OSError as e:
        log(f"ERROR: can't bind TCP {args.listen_port} ({e}).")
        log("       Pick a free --listen-port (e.g. 11555), then retry.")
        sys.exit(1)

    stop = threading.Event()
    threading.Thread(target=discovery_responder, args=(usock, args.listen_port, stop),
                     daemon=True).start()
    log(f"Alpaca discovery proxy up: NINA -> this host:{args.listen_port} -> "
        f"{args.pi}:{args.pi_port}.  Ctrl+C to stop.")
    try:
        tcp_proxy(lsock, args.pi, args.pi_port, stop)
    except KeyboardInterrupt:
        log("stopping.")
    finally:
        stop.set()
        for s in (usock, lsock):
            try:
                s.close()
            except OSError:
                pass


if __name__ == "__main__":
    main()
