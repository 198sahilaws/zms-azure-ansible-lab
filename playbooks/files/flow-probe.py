#!/usr/bin/env python3
"""flow-probe.py - run a list of network probes from this host and print JSON results.

Used by the flow-test playbooks through the `script` module. Standard library only;
runs on Python 3.6+ (RHEL 8 / SLES 15 use python3.11 via osver_* group_vars).

Usage:  flow-probe.py <base64 of a JSON document>

  {"timeout": 5, "rounds": 1, "interval": 30, "enforced": false,
   "probes": [{"flow": "F1", "target": "web", "address": "10.193.10.4", "port": 8090,
               "probe": "http", "path": "/api/tickets", "expect": "allow"},
              {"flow": "F10", "target": "github.com", "url": "https://github.com/",
               "probe": "https", "expect": "allow", "verify": true, "zscaler": false}]}

Probe kinds (each opens a real TCP connection from this host, then):
  tcp     nothing more
  ssh     reads the server banner, sends a client identification string (no login)
  rdp     X.224 Connection Request + RDP negotiation, reads the Connection Confirm (no login)
  mysql   reads the server greeting (no login)
  http    one GET, reads the status line
  https   TLS handshake (certificate checked unless "verify": false), then one GET

Result per probe: OPEN (connected), REFUSED (reset), TIMEOUT (dropped), ERROR (DNS, unreachable...).
Verdict: allow -> PASS if OPEN else FAIL; deny -> PASS unless OPEN, which is INFO before
enforcement and FAIL with "enforced": true.
"""
import base64
import datetime
import json
import re
import socket
import ssl
import struct
import sys
import time
from urllib.parse import urlsplit

USER_AGENT = "zms-flow-probe/1.0"


class Handshake(Exception):
    """Connected, but the protocol exchange did not complete."""


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(host, port, timeout):
    """Return (status, socket_or_None, detail)."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        return "OPEN", sock, "TCP connect"
    except socket.timeout:
        return "TIMEOUT", None, "no answer in %ss" % timeout
    except ConnectionRefusedError:
        return "REFUSED", None, "connection refused (reset)"
    except ConnectionResetError:
        return "REFUSED", None, "connection reset"
    except socket.gaierror as exc:
        return "ERROR", None, "DNS: %s" % exc
    except OSError as exc:
        return "ERROR", None, exc.strerror or str(exc)


def recv_some(sock, limit):
    data = b""
    try:
        while len(data) < limit:
            chunk = sock.recv(min(65536, limit - len(data)))
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        if not data:
            raise Handshake("no reply before timeout")
    return data


def do_ssh(sock):
    buf = b""
    while b"\n" not in buf or not re.search(rb"(^|\n)SSH-", buf):
        chunk = sock.recv(1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 8192:
            break
    match = re.search(rb"(?:^|\n)(SSH-[^\r\n]*)", buf)
    if not match:
        raise Handshake("no SSH version banner received")
    sock.sendall(b"SSH-2.0-ZMS_FlowProbe\r\n")
    return "SSH banner: %s" % match.group(1).decode("ascii", "replace")


RDP_PROTOCOLS = {0: "standard RDP", 1: "TLS", 2: "CredSSP (NLA)", 4: "RDSTLS", 8: "CredSSP early auth"}


def do_rdp(sock):
    # TPKT + X.224 Connection Request + RDP_NEG_REQ (requestedProtocols = TLS | CredSSP)
    sock.sendall(bytes([0x03, 0x00, 0x00, 0x13,
                        0x0E, 0xE0, 0x00, 0x00, 0x00, 0x00, 0x00,
                        0x01, 0x00, 0x08, 0x00, 0x03, 0x00, 0x00, 0x00]))
    reply = sock.recv(64)
    if len(reply) < 7 or reply[0] != 0x03 or reply[5] != 0xD0:
        raise Handshake("no X.224 Connection Confirm (%d bytes received)" % len(reply))
    if len(reply) >= 19 and reply[11] == 0x02:
        proto = struct.unpack("<I", reply[15:19])[0]
        return "RDP connection confirm, server selected %s" % RDP_PROTOCOLS.get(proto, "protocol %d" % proto)
    if len(reply) >= 19 and reply[11] == 0x03:
        return "RDP connection confirm, negotiation failure code %d" % struct.unpack("<I", reply[15:19])[0]
    return "RDP connection confirm"


def do_mysql(sock):
    header = recv_exact(sock, 4)
    length = header[0] | (header[1] << 8) | (header[2] << 16)
    payload = recv_exact(sock, min(length, 4096))
    if payload[:1] == b"\xff":
        message = payload[3:].decode("utf-8", "replace")
        return "MySQL answered with an error packet: %s" % message.lstrip("#").strip()
    version = payload[1:].split(b"\x00", 1)[0].decode("ascii", "replace")
    return "MySQL greeting, server %s" % version


def recv_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise Handshake("connection closed after %d of %d bytes" % (len(data), count))
        data += chunk
    return data


def do_http(sock, host_header, path, want_body):
    request = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\nAccept: */*\r\n"
               "Connection: close\r\n\r\n" % (path, host_header, USER_AGENT))
    sock.sendall(request.encode("ascii"))
    data = recv_some(sock, 262144 if want_body else 8192)
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", "replace").split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        raise Handshake("no HTTP status line")
    detail = lines[0]
    for line in lines[1:]:
        if line.lower().startswith("location:"):
            detail += " -> " + line.split(":", 1)[1].strip()
            break
    return detail, body


def zscaler_status(body):
    """Summarise ip.zscaler.com. The page wording is matched loosely; unknown wording is reported as such."""
    text = re.sub(r"<[^>]+>", " ", body.decode("utf-8", "replace"))
    text = re.sub(r"\s+", " ", text)
    if re.search(r"not going through the Zscaler", text, re.I):
        verdict = "not via Zscaler"
    elif re.search(r"(accessing this host via|going through) (a |the )?Zscaler", text, re.I):
        verdict = "via Zscaler"
    else:
        verdict = "Zscaler status not recognised"
    ip = re.search(r"IP address[^0-9]{0,40}(\d{1,3}(?:\.\d{1,3}){3})", text, re.I)
    return verdict + (", egress IP %s" % ip.group(1) if ip else "")


def run_probe(p, timeout):
    kind = p.get("probe", "tcp")
    path = p.get("path", "/")
    if p.get("url"):
        parts = urlsplit(p["url"])
        kind = parts.scheme or kind
        host = parts.hostname
        port = parts.port or (443 if kind == "https" else 80)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        default_port = 443 if kind == "https" else 80
        host_header = host if port == default_port else "%s:%d" % (host, port)
    else:
        host = p["address"]
        port = int(p["port"])
        host_header = "%s:%d" % (host, port)

    status, sock, detail = connect(host, port, timeout)
    if sock is None:
        return host, port, status, detail
    try:
        if kind == "ssh":
            detail = do_ssh(sock)
        elif kind == "rdp":
            detail = do_rdp(sock)
        elif kind == "mysql":
            detail = do_mysql(sock)
        elif kind == "http":
            detail, _ = do_http(sock, host_header, path, False)
        elif kind == "https":
            context = ssl.create_default_context()
            if not p.get("verify", True):
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            try:
                tls = context.wrap_socket(sock, server_hostname=host)
            except ssl.SSLError as exc:
                reason = "certificate verify failed" if "CERTIFICATE_VERIFY_FAILED" in str(exc) else str(exc)
                return host, port, status, "TCP connected; TLS failed: %s" % reason
            except ssl.CertificateError as exc:
                return host, port, status, "TCP connected; TLS failed: %s" % exc
            sock = tls
            tls_version = tls.version() or "TLS"
            detail, body = do_http(sock, host_header, path, bool(p.get("zscaler")))
            detail = "%s %s" % (tls_version, detail)
            if p.get("zscaler"):
                detail += "; " + zscaler_status(body)
    except (Handshake, OSError, ValueError) as exc:
        detail = "TCP connected; handshake failed: %s" % exc
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return host, port, status, detail


def verdict(expect, status, enforced):
    if expect == "allow":
        return "PASS" if status == "OPEN" else "FAIL"
    if status == "OPEN":
        return "FAIL" if enforced else "INFO"
    return "PASS"


def main():
    if len(sys.argv) != 2:
        sys.stderr.write("usage: flow-probe.py <base64 JSON plan>\n")
        return 2
    plan = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    timeout = float(plan.get("timeout", 5))
    rounds = max(1, int(plan.get("rounds", 1)))
    interval = max(0, int(plan.get("interval", 30)))
    enforced = bool(plan.get("enforced", False))
    me = socket.gethostname()

    results = []
    for rnd in range(1, rounds + 1):
        for p in plan.get("probes", []):
            started = time.monotonic()
            host, port, status, detail = run_probe(p, timeout)
            expect = p.get("expect", "allow")
            results.append({
                "Time": now_iso(),
                "Round": rnd,
                "Flow": p.get("flow", ""),
                "Source": me,
                "Target": p.get("target", host),
                "Address": host,
                "Port": port,
                "Expect": expect,
                "Result": status,
                "Verdict": verdict(expect, status, enforced),
                "Ms": int((time.monotonic() - started) * 1000),
                "Detail": detail,
            })
        if rnd < rounds and interval:
            time.sleep(interval)

    json.dump(results, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
