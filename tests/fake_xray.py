"""Minimal stand-in for the xray binary, used by test_xray_manager.py.

Supports: ``version``, ``run -test -c FILE``, ``run -c FILE`` (listens on the
API port and every inbound port) and ``api adi|ado|adrules|rmi|rmo|rmrules``
(forwarded to the running instance over its API port).  An outbound whose
protocol is "invalid" is rejected like real Xray would.
"""

import json
import socket
import sys


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def invalid(config):
    return any(o.get("protocol") == "invalid" for o in config.get("outbounds", []))


def listen(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
    return sock


def serve(config):
    api_port = int(config["api"]["listen"].rsplit(":", 1)[1])
    inbounds = {ib["tag"]: listen(ib["port"]) for ib in config.get("inbounds", [])}
    api = listen(api_port)
    print("Xray 25.1.1 started", flush=True)
    while True:
        conn, _ = api.accept()
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            if not data:
                continue  # readiness probe
            message = json.loads(data)
            command, payload = message["cmd"], message["payload"]
            reply = b"ok\n"
            if command == "ado" and invalid(payload):
                reply = b"failed: unknown protocol\n"
            elif command == "adi":
                for ib in payload["inbounds"]:
                    inbounds[ib["tag"]] = listen(ib["port"])
            elif command == "rmi":
                for tag in payload:
                    sock = inbounds.pop(tag, None)
                    if sock:
                        sock.close()
            conn.sendall(reply)


def api_call(args):
    command = args[0]
    server = next(a.split("=", 1)[1] for a in args if a.startswith("--server="))
    rest = [a for a in args[1:] if not a.startswith("--server=") and a != "-append"]
    payload = load(rest[0]) if command in ("adi", "ado", "adrules") else rest
    host, port = server.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=5) as conn:
        conn.sendall(json.dumps({"cmd": command, "payload": payload}).encode() + b"\n")
        reply = conn.recv(1024).decode()
    print(reply.strip())
    return 0 if reply.startswith("ok") else 1


def main(args):
    if args[0] == "version":
        print("Xray 25.1.1 (Xray, Penetrates Everything.) fake (go1.25 linux/amd64)")
        return 0
    if args[0] == "run":
        config = load(args[args.index("-c") + 1])
        if "-test" in args:
            if invalid(config):
                print("Failed to start: infra/conf: unknown protocol: invalid")
                return 1
            return 0
        serve(config)
    if args[0] == "api":
        return api_call(args[1:])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
