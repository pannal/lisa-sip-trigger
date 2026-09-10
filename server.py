#!/usr/bin/env python3
import json
import hmac
import logging
import math
import os
import random
import re
import signal
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def env_float(name, default):
    return float(os.getenv(name, str(default)))


def env_int(name, default):
    return int(os.getenv(name, str(default)))


ATA_HOST = os.getenv("ATA_HOST", os.getenv("SPA_HOST", ""))
ATA_PORT = int(os.getenv("ATA_PORT", os.getenv("SPA_PORT", "5061")))
ATA_USER = os.getenv("ATA_USER", os.getenv("SPA_USER", "lisa"))
RING_SECONDS = env_float("RING_SECONDS", 2.0)
RETRIGGER_INTERVAL = env_float("RETRIGGER_INTERVAL", 32.0)
MAX_ALARM_SECONDS = env_float("MAX_ALARM_SECONDS", 1800.0)
ERROR_RETRY_SECONDS = env_float("ERROR_RETRY_SECONDS", 5.0)
HTTP_BIND = os.getenv("HTTP_BIND", "0.0.0.0")
HTTP_PORT = env_int("HTTP_PORT", 18080)
API_TOKEN = os.getenv("API_TOKEN", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
INVITE_TIMEOUT = 5.0
CANCEL_TIMEOUT = 3.0

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("lisa-trigger")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def local_ip_for(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, port))
        return s.getsockname()[0]
    finally:
        s.close()


def status_code(message):
    first = message.splitlines()[0] if message else ""
    parts = first.split()
    if len(parts) >= 2 and parts[0].startswith("SIP/"):
        try:
            return int(parts[1])
        except ValueError:
            pass
    return None


def get_header(message, name):
    prefix = name.lower() + ":"
    for line in message.splitlines():
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


class SipCall:
    def __init__(self, host, port, user, stop_event):
        self.host = host
        self.port = port
        self.user = user
        self.stop_event = stop_event
        self.remote = (socket.gethostbyname(host), port)
        self.local_ip = local_ip_for(host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((self.local_ip, 0))
            self.sock.settimeout(0.20)
        except OSError:
            self.sock.close()
            raise
        self.local_port = self.sock.getsockname()[1]
        self.branch = "z9hG4bK-" + uuid.uuid4().hex
        self.from_tag = uuid.uuid4().hex[:12]
        self.call_id = f"{uuid.uuid4().hex}@{self.local_ip}"
        self.cseq = random.randint(1000, 9999)
        self.uri = f"sip:{self.user}@{self.host}:{self.port}"
        self.to_header = f"<sip:{self.user}@{self.host}>"
        self.original_to = self.to_header
        self.dialog_uri = self.uri
        self.got_provisional = False
        self.cancelled = False

    def _make_sdp(self):
        now = int(time.time())
        return (
            "v=0\r\n"
            f"o=- {now} {now} IN IP4 {self.local_ip}\r\n"
            "s=LISA trigger\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 0 8 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=sendrecv\r\n"
        )

    def _make_invite(self):
        sdp = self._make_sdp()
        return (
            f"INVITE {self.uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={self.branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:ha@{self.local_ip}>;tag={self.from_tag}\r\n"
            f"To: <sip:{self.user}@{self.host}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} INVITE\r\n"
            f"Contact: <sip:ha@{self.local_ip}:{self.local_port}>\r\n"
            "User-Agent: HA-LISA-Trigger/1.0\r\n"
            "Allow: INVITE, ACK, CANCEL, BYE\r\n"
            "Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp.encode())}\r\n"
            "\r\n"
            f"{sdp}"
        )

    def _make_cancel(self):
        return (
            f"CANCEL {self.uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={self.branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:ha@{self.local_ip}>;tag={self.from_tag}\r\n"
            f"To: {self.original_to}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} CANCEL\r\n"
            "User-Agent: HA-LISA-Trigger/1.0\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    def _make_ack(self, to_header):
        return (
            f"ACK {self.uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={self.branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:ha@{self.local_ip}>;tag={self.from_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} ACK\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    def _make_ack_2xx(self, to_header):
        branch = "z9hG4bK-" + uuid.uuid4().hex
        return (
            f"ACK {self.dialog_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:ha@{self.local_ip}>;tag={self.from_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} ACK\r\n"
            f"Contact: <sip:ha@{self.local_ip}:{self.local_port}>\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    def _make_bye(self, to_header):
        branch = "z9hG4bK-" + uuid.uuid4().hex
        return (
            f"BYE {self.dialog_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:ha@{self.local_ip}>;tag={self.from_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq + 1} BYE\r\n"
            f"Contact: <sip:ha@{self.local_ip}:{self.local_port}>\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    def _recv(self):
        try:
            data, peer = self.sock.recvfrom(65535)
        except socket.timeout:
            return None
        msg = data.decode(errors="replace")
        if peer != self.remote or get_header(msg, "Call-ID") != self.call_id:
            return None
        cseq = (get_header(msg, "CSeq") or "").split()
        if cseq not in ([str(self.cseq), "INVITE"], [str(self.cseq), "CANCEL"],
                        [str(self.cseq + 1), "BYE"]):
            return None
        code = status_code(msg)
        if code:
            log.debug("SIP <- %s", msg.splitlines()[0])
        received_to = get_header(msg, "To")
        if received_to:
            self.to_header = received_to
        return msg

    def _cleanup_answered_call(self, msg):
        to_header = get_header(msg, "To") or self.to_header
        contact = get_header(msg, "Contact") or ""
        target = re.search(r"sip:[^<>\s]+", contact)
        if target:
            self.dialog_uri = target.group(0)
        # No proxy route set is used in this direct-ATA client. In-dialog
        # requests use the Contact target, which can differ from the INVITE.
        authority = self.dialog_uri[4:].split(";", 1)[0].rsplit("@", 1)[-1]
        host, separator, port = authority.partition(":")
        dialog_remote = (socket.gethostbyname(host), int(port) if separator else 5060)
        log.warning("ATA answered INVITE; sending ACK + BYE")
        self.sock.sendto(self._make_ack_2xx(to_header).encode(), dialog_remote)
        self.sock.sendto(self._make_bye(to_header).encode(), dialog_remote)

    def cancel(self):
        if self.cancelled:
            return
        self.cancelled = True
        if not self.got_provisional:
            return

        cancel = self._make_cancel().encode()
        self.sock.sendto(cancel, self.remote)
        log.debug("SIP -> CANCEL")
        got_cancel_ok = False
        deadline = time.monotonic() + CANCEL_TIMEOUT
        next_retry = time.monotonic() + 0.5
        retry_interval = 0.5

        while time.monotonic() < deadline:
            msg = self._recv()
            if msg:
                code = status_code(msg)
                cseq = get_header(msg, "CSeq") or ""
                if code == 200 and "CANCEL" in cseq.upper():
                    got_cancel_ok = True
                elif code is not None and code >= 300 and cseq == f"{self.cseq} INVITE":
                    to_header = get_header(msg, "To") or self.to_header
                    self.sock.sendto(self._make_ack(to_header).encode(), self.remote)
                    # A final rejection (normally 487) terminates the INVITE.
                    return
                elif code is not None and 200 <= code < 300 and cseq == f"{self.cseq} INVITE":
                    self._cleanup_answered_call(msg)
                    return

            now = time.monotonic()
            if now >= next_retry and not got_cancel_ok:
                self.sock.sendto(cancel, self.remote)
                retry_interval = min(retry_interval * 2.0, 1.0)
                next_retry = now + retry_interval

        raise RuntimeError("No final INVITE response after SIP CANCEL")

    def ring(self, ring_seconds):
        invite = self._make_invite().encode()
        deadline = time.monotonic() + INVITE_TIMEOUT
        next_retry = time.monotonic() + 0.5
        retry_interval = 0.5
        ringing = False

        try:
            if self.stop_event.is_set():
                return False
            log.info("Calling %s from %s:%s", self.uri, self.local_ip, self.local_port)
            self.sock.sendto(invite, self.remote)
            while time.monotonic() < deadline:
                # RFC 3261 requires a provisional response before CANCEL.
                # On early STOP keep listening (without retransmitting INVITE)
                # so a delayed provisional response can still be cancelled.
                if self.stop_event.is_set() and self.got_provisional:
                    self.cancel()
                    return False

                msg = self._recv()
                if msg:
                    if get_header(msg, "CSeq") != f"{self.cseq} INVITE":
                        continue
                    code = status_code(msg)
                    if code is None:
                        continue
                    if 100 <= code < 200:
                        self.got_provisional = True
                    if code == 180:
                        ringing = True
                        log.info("ATA is ringing")
                        break
                    if 200 <= code < 300:
                        self._cleanup_answered_call(msg)
                        raise RuntimeError("INVITE was answered unexpectedly")
                    if code >= 300:
                        self.sock.sendto(self._make_ack(self.to_header).encode(), self.remote)
                        raise RuntimeError(f"ATA rejected INVITE: {msg.splitlines()[0]}")

                now = time.monotonic()
                if not self.stop_event.is_set() and not self.got_provisional and now >= next_retry:
                    self.sock.sendto(invite, self.remote)
                    retry_interval = min(retry_interval * 2.0, 2.0)
                    next_retry = now + retry_interval

            if not ringing:
                self.cancel()
                if self.stop_event.is_set():
                    return False
                raise RuntimeError("No 180 Ringing response from ATA")

            ring_deadline = time.monotonic() + ring_seconds
            while not self.stop_event.is_set() and time.monotonic() < ring_deadline:
                self.sock.settimeout(min(0.20, max(0.001, ring_deadline - time.monotonic())))
                msg = self._recv()
                if not msg or get_header(msg, "CSeq") != f"{self.cseq} INVITE":
                    continue
                code = status_code(msg)
                if code is not None and 200 <= code < 300:
                    self._cleanup_answered_call(msg)
                    raise RuntimeError("INVITE was answered unexpectedly")
                if code is not None and code >= 300:
                    self.sock.sendto(self._make_ack(self.to_header).encode(), self.remote)
                    raise RuntimeError(f"ATA rejected INVITE: {msg.splitlines()[0]}")
            self.sock.settimeout(0.20)
            stopped = self.stop_event.is_set()
            if stopped:
                log.info("Stop requested while PHONE 2 was ringing")
            self.cancel()
            return not stopped
        finally:
            self.sock.close()


class AlarmController:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop_event = None
        self.started_at = None
        self.stopped_at = None
        self.cycles_sent = 0
        self.last_cycle_at = None
        self.last_success_at = None
        self.last_error = None
        self.run_id = None
        self.shutting_down = False

    def _thread_alive(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        with self.lock:
            if self.shutting_down:
                return False, "shutting_down"
            if self._thread_alive():
                if self.stop_event is not None and not self.stop_event.is_set():
                    return False, "already_active"
                return False, "still_stopping"
            self.stop_event = threading.Event()
            self.started_at = utc_now()
            self.stopped_at = None
            self.cycles_sent = 0
            self.last_cycle_at = None
            self.last_success_at = None
            self.last_error = None
            self.run_id = uuid.uuid4().hex[:12]
            self.thread = threading.Thread(
                target=self._run,
                name=f"lisa-alarm-{self.run_id}",
                daemon=False,
            )
            self.thread.start()
            return True, "started"

    def stop(self):
        with self.lock:
            if not self._thread_alive():
                return False, "already_stopped"
            if self.stop_event.is_set():
                return False, "stopping"
            self.stop_event.set()
            self.stopped_at = utc_now()
            return True, "stopping"

    def shutdown(self):
        with self.lock:
            self.shutting_down = True
            if self.stop_event is not None:
                self.stop_event.set()
            thread = self.thread
        if thread is not None:
            thread.join()

    def _set_error(self, text):
        with self.lock:
            self.last_error = text

    def _run(self):
        stop_event = self.stop_event
        monotonic_started = time.monotonic()
        next_cycle_at = monotonic_started
        # A per-run timer interrupts ringing, retrigger waits, and error backoff.
        # It captures this run's event so it cannot stop a later run.
        failsafe = None
        if MAX_ALARM_SECONDS > 0:
            def expire():
                log.warning("Failsafe maximum alarm duration reached")
                stop_event.set()
            failsafe = threading.Timer(MAX_ALARM_SECONDS, expire)
            failsafe.daemon = True
            failsafe.start()
        log.info(
            "Alarm started: target=%s:%s user=%s ring=%.1fs interval=%.1fs max=%.0fs",
            ATA_HOST, ATA_PORT, ATA_USER, RING_SECONDS,
            RETRIGGER_INTERVAL, MAX_ALARM_SECONDS,
        )

        try:
            while not stop_event.is_set():
                elapsed = time.monotonic() - monotonic_started
                if MAX_ALARM_SECONDS > 0 and elapsed >= MAX_ALARM_SECONDS:
                    log.warning("Failsafe maximum alarm duration reached")
                    break

                wait_for = next_cycle_at - time.monotonic()
                if wait_for > 0 and stop_event.wait(timeout=wait_for):
                    break
                if stop_event.is_set():
                    break

                cycle_started = time.monotonic()
                with self.lock:
                    self.cycles_sent += 1
                    cycle_number = self.cycles_sent
                    self.last_cycle_at = utc_now()
                log.info("LISA trigger cycle %d", cycle_number)

                try:
                    call = SipCall(ATA_HOST, ATA_PORT, ATA_USER, stop_event)
                    completed = call.ring(RING_SECONDS)
                    if completed:
                        with self.lock:
                            self.last_success_at = utc_now()
                            self.last_error = None
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    log.exception("Trigger cycle failed: %s", message)
                    self._set_error(message)
                    if stop_event.wait(timeout=ERROR_RETRY_SECONDS):
                        break
                    next_cycle_at = time.monotonic()
                    continue

                next_cycle_at = cycle_started + RETRIGGER_INTERVAL
        finally:
            if failsafe is not None:
                failsafe.cancel()
                failsafe.join()
            with self.lock:
                self.stopped_at = utc_now()
            log.info("Alarm stopped")

    def status(self):
        with self.lock:
            active = self._thread_alive() and self.stop_event is not None and not self.stop_event.is_set()
            stopping = self._thread_alive() and self.stop_event is not None and self.stop_event.is_set()
            return {
                "active": active,
                "stopping": stopping,
                "run_id": self.run_id,
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "cycles_sent": self.cycles_sent,
                "last_cycle_at": self.last_cycle_at,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
                "config": {
                    "ata_host": ATA_HOST,
                    "ata_port": ATA_PORT,
                    "ata_user": ATA_USER,
                    # Retained for existing /status consumers.
                    "spa_host": ATA_HOST,
                    "spa_port": ATA_PORT,
                    "spa_user": ATA_USER,
                    "ring_seconds": RING_SECONDS,
                    "retrigger_interval": RETRIGGER_INTERVAL,
                    "max_alarm_seconds": MAX_ALARM_SECONDS,
                },
            }


controller = AlarmController()


class Handler(BaseHTTPRequestHandler):
    server_version = "LISA-Trigger/1.0"

    def log_message(self, fmt, *args):
        log.info("HTTP %s - %s", self.client_address[0], fmt % args)

    def _authorized(self):
        if not API_TOKEN:
            return True
        bearer = self.headers.get("Authorization", "")
        api_key = self.headers.get("X-API-Key", "")
        return (hmac.compare_digest(bearer.encode(), f"Bearer {API_TOKEN}".encode())
                or hmac.compare_digest(api_key.encode(), API_TOKEN.encode()))

    def _json(self, status, payload):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        if path == "/status":
            self._json(200, controller.status())
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self):
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        if path == "/alarm/start":
            changed, result = controller.start()
            status = 202 if changed else (200 if result == "already_active" else 409)
            self._json(status, {"result": result, **controller.status()})
            return
        if path == "/alarm/stop":
            changed, result = controller.stop()
            self._json(200, {"result": result, **controller.status()})
            return
        self._json(404, {"error": "not_found"})


def validate_config():
    if not ATA_HOST or not re.fullmatch(r"[A-Za-z0-9.-]+", ATA_HOST):
        raise ValueError("ATA_HOST (or SPA_HOST) must be an IPv4 address or hostname")
    if not ATA_USER or not re.fullmatch(r"[A-Za-z0-9_.!~*'()+%-]+", ATA_USER):
        raise ValueError("ATA_USER must be a SIP user without whitespace or header delimiters")
    if not 1 <= ATA_PORT <= 65535:
        raise ValueError("ATA_PORT must be 1..65535")
    if not 1 <= HTTP_PORT <= 65535:
        raise ValueError("HTTP_PORT must be 1..65535")
    for name in ("RING_SECONDS", "RETRIGGER_INTERVAL", "MAX_ALARM_SECONDS", "ERROR_RETRY_SECONDS"):
        if not math.isfinite(globals()[name]):
            raise ValueError(f"{name} must be finite")
    if MAX_ALARM_SECONDS < 0:
        raise ValueError("MAX_ALARM_SECONDS must be >= 0")
    if RING_SECONDS <= 0:
        raise ValueError("RING_SECONDS must be > 0")
    if RETRIGGER_INTERVAL <= RING_SECONDS:
        raise ValueError("RETRIGGER_INTERVAL must be greater than RING_SECONDS")
    if ERROR_RETRY_SECONDS <= 0:
        raise ValueError("ERROR_RETRY_SECONDS must be > 0")


def main():
    validate_config()
    log.info("Listening on http://%s:%s", HTTP_BIND, HTTP_PORT)
    httpd = ThreadingHTTPServer((HTTP_BIND, HTTP_PORT), Handler)
    httpd.timeout = 0.20
    shutdown_requested = threading.Event()

    def request_shutdown(signum, frame):
        shutdown_requested.set()

    previous_handlers = {
        signum: signal.signal(signum, request_shutdown)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        while not shutdown_requested.is_set():
            httpd.handle_request()
    finally:
        log.info("Shutting down; waiting for SIP cleanup")
        controller.shutdown()
        httpd.server_close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
