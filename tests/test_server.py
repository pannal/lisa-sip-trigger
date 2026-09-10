"""Protocol and lifecycle checks using real loopback UDP/HTTP sockets only."""
import contextlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import server

ROOT = Path(__file__).resolve().parents[1]


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Condition did not become true before timeout")


class FakeATA:
    def __init__(self, behavior=None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.messages = []
        self.errors = []
        self.stop_event = threading.Event()
        self.behavior = behavior or self.normal
        self.thread = threading.Thread(target=self.run)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        self.thread.join(2)
        self.sock.close()
        if self.errors:
            raise AssertionError(self.errors)

    def run(self):
        while not self.stop_event.is_set():
            try:
                data, peer = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            message = data.decode()
            self.messages.append(message)
            try:
                self.behavior(self, message, peer)
            except Exception as exc:
                self.errors.append(exc)
                return

    def methods(self):
        return [msg.split()[0] for msg in self.messages]

    def response(self, request, peer, status, method=None, call_id=None, extra=""):
        cseq = server.get_header(request, "CSeq")
        if method:
            cseq = cseq.split()[0] + " " + method
        to = server.get_header(request, "To")
        if ";tag=" not in to:
            to += ";tag=fake-ata"
        message = (
            f"SIP/2.0 {status}\r\n"
            f"Via: {server.get_header(request, 'Via')}\r\n"
            f"From: {server.get_header(request, 'From')}\r\n"
            f"To: {to}\r\n"
            f"Call-ID: {call_id or server.get_header(request, 'Call-ID')}\r\n"
            f"CSeq: {cseq}\r\n{extra}Content-Length: 0\r\n\r\n"
        )
        self.sock.sendto(message.encode(), peer)

    @staticmethod
    def normal(ata, msg, peer):
        method = msg.split()[0]
        if method == "INVITE":
            ata.response(msg, peer, "100 Trying")
            ata.response(msg, peer, "180 Ringing")
        elif method == "CANCEL":
            ata.response(msg, peer, "200 OK")
            ata.response(msg, peer, "487 Request Terminated", method="INVITE")
        elif method == "BYE":
            ata.response(msg, peer, "200 OK")


@contextlib.contextmanager
def configured(ata, **overrides):
    values = dict(ATA_HOST="127.0.0.1", ATA_PORT=ata.port, ATA_USER="lisa",
                  RING_SECONDS=0.06, RETRIGGER_INTERVAL=30.0,
                  MAX_ALARM_SECONDS=0, ERROR_RETRY_SECONDS=0.05,
                  INVITE_TIMEOUT=0.8, CANCEL_TIMEOUT=0.8, API_TOKEN="")
    values.update(overrides)
    with patch.multiple(server, **values):
        controller = server.AlarmController()
        with patch.object(server, "controller", controller):
            try:
                yield controller
            finally:
                controller.shutdown()


class SipTests(unittest.TestCase):
    def call(self, ata, event=None):
        return server.SipCall("127.0.0.1", ata.port, "lisa", event or threading.Event())

    def assert_transaction(self, ata):
        wait_until(lambda: "ACK" in ata.methods())
        invite, cancel, ack = [next(m for m in ata.messages if m.startswith(method + " "))
                               for method in ("INVITE", "CANCEL", "ACK")]
        for name in ("Via", "From", "Call-ID"):
            self.assertEqual(server.get_header(invite, name), server.get_header(cancel, name))
            self.assertEqual(server.get_header(invite, name), server.get_header(ack, name))
        self.assertEqual(server.get_header(invite, "To"), server.get_header(cancel, "To"))
        self.assertIn(";tag=fake-ata", server.get_header(ack, "To"))
        for msg in (cancel, ack):
            self.assertEqual(invite.split()[1], msg.split()[1])
            self.assertEqual(server.get_header(invite, "CSeq").split()[0],
                             server.get_header(msg, "CSeq").split()[0])

    def test_ring_cancel_ack(self):
        with FakeATA() as ata:
            self.assertTrue(self.call(ata).ring(0.05))
            self.assert_transaction(ata)

    def test_rejection_is_acknowledged(self):
        def reject(ata, msg, peer):
            if msg.startswith("INVITE "):
                ata.response(msg, peer, "486 Busy Here")
        with FakeATA(reject) as ata:
            with self.assertRaisesRegex(RuntimeError, "486 Busy Here"):
                self.call(ata).ring(1)
            wait_until(lambda: "ACK" in ata.methods())
            self.assertNotIn("CANCEL", ata.methods())

    def test_rejection_after_ringing_is_acknowledged(self):
        def reject(ata, msg, peer):
            if msg.startswith("INVITE "):
                ata.response(msg, peer, "180 Ringing")
                ata.response(msg, peer, "503 Service Unavailable")
        with FakeATA(reject) as ata:
            with self.assertRaisesRegex(RuntimeError, "503"):
                self.call(ata).ring(1)
            wait_until(lambda: "ACK" in ata.methods())
            self.assertNotIn("CANCEL", ata.methods())

    def test_no_response_retries_and_times_out(self):
        with FakeATA(lambda *args: None) as ata, configured(ata):
            call = self.call(ata)
            with self.assertRaisesRegex(RuntimeError, "No 180"):
                call.ring(0.05)
            self.assertGreaterEqual(ata.methods().count("INVITE"), 2)
            self.assertNotIn("CANCEL", ata.methods())
            self.assertEqual(call.sock.fileno(), -1)

    def test_cancel_retransmission_preserves_message(self):
        def drop_first_cancel(ata, msg, peer):
            if msg.startswith("CANCEL ") and ata.methods().count("CANCEL") == 1:
                return
            FakeATA.normal(ata, msg, peer)
        with FakeATA(drop_first_cancel) as ata:
            self.assertTrue(self.call(ata).ring(0.05))
            cancels = [m for m in ata.messages if m.startswith("CANCEL ")]
            self.assertEqual(len(cancels), 2)
            self.assertEqual(*cancels)
            self.assert_transaction(ata)

    def test_cancel_accepts_other_invite_final_responses(self):
        def reject_during_cancel(ata, msg, peer):
            if msg.startswith("CANCEL "):
                ata.response(msg, peer, "200 OK")
                ata.response(msg, peer, "486 Busy Here", method="INVITE")
            else:
                FakeATA.normal(ata, msg, peer)
        with FakeATA(reject_during_cancel) as ata:
            self.assertTrue(self.call(ata).ring(0.05))
            self.assert_transaction(ata)

    def test_cancel_ok_without_invite_final_is_error(self):
        def missing_final(ata, msg, peer):
            if msg.startswith("CANCEL "):
                ata.response(msg, peer, "200 OK")
            else:
                FakeATA.normal(ata, msg, peer)
        with FakeATA(missing_final) as ata, configured(ata, CANCEL_TIMEOUT=0.3):
            with self.assertRaisesRegex(RuntimeError, "No final INVITE"):
                self.call(ata).ring(0.05)

    def test_provisional_without_ringing_is_cancelled_on_timeout(self):
        def trying_only(ata, msg, peer):
            if msg.startswith("INVITE "):
                ata.response(msg, peer, "100 Trying")
            else:
                FakeATA.normal(ata, msg, peer)
        with FakeATA(trying_only) as ata, configured(ata, INVITE_TIMEOUT=0.3):
            with self.assertRaisesRegex(RuntimeError, "No 180"):
                self.call(ata).ring(0.05)
            self.assert_transaction(ata)

    def test_final_before_cancel_ok(self):
        def reordered(ata, msg, peer):
            if msg.startswith("CANCEL "):
                ata.response(msg, peer, "487 Request Terminated", method="INVITE")
                ata.response(msg, peer, "200 OK")
            else:
                FakeATA.normal(ata, msg, peer)
        with FakeATA(reordered) as ata:
            self.assertTrue(self.call(ata).ring(0.05))
            self.assert_transaction(ata)

    def test_stop_before_provisional_cancels_delayed_ringing(self):
        stop = threading.Event()
        def delayed(ata, msg, peer):
            if msg.startswith("INVITE "):
                stop.set()
                time.sleep(0.25)
            FakeATA.normal(ata, msg, peer)
        with FakeATA(delayed) as ata:
            self.assertFalse(self.call(ata, stop).ring(10))
            self.assert_transaction(ata)
            self.assertEqual(ata.methods().count("INVITE"), 1)

    def test_preexisting_stop_sends_nothing(self):
        stop = threading.Event()
        stop.set()
        with FakeATA() as ata:
            self.assertFalse(self.call(ata, stop).ring(10))
            self.assertEqual(ata.methods(), [])

    def test_wrong_call_and_cseq_are_ignored(self):
        def unrelated(ata, msg, peer):
            if msg.startswith("INVITE "):
                ata.response(msg, peer, "486 Busy Here", call_id="another-call")
                ata.response(msg, peer, "200 OK", method="CANCEL")
            FakeATA.normal(ata, msg, peer)
        with FakeATA(unrelated) as ata:
            self.assertTrue(self.call(ata).ring(0.05))
            self.assert_transaction(ata)

    def test_answered_invite_ack_bye(self):
        for during_cancel in (False, True):
            with self.subTest(during_cancel=during_cancel):
                def answer(ata, msg, peer):
                    if msg.startswith("INVITE "):
                        if during_cancel:
                            FakeATA.normal(ata, msg, peer)
                        else:
                            ata.response(msg, peer, "180 Ringing")
                            ata.response(msg, peer, "200 OK", extra=f"Contact: <sip:answered@127.0.0.1:{ata.port}>\r\n")
                    elif msg.startswith("CANCEL "):
                        ata.response(msg, peer, "200 OK", method="INVITE",
                                     extra=f"Contact: <sip:answered@127.0.0.1:{ata.port}>\r\n")
                with FakeATA(answer) as ata:
                    if during_cancel:
                        self.call(ata).ring(0.05)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "answered unexpectedly"):
                            self.call(ata).ring(5)
                    wait_until(lambda: "BYE" in ata.methods())
                    invite = ata.messages[0]
                    ack, bye = [next(m for m in ata.messages if m.startswith(k + " ")) for k in ("ACK", "BYE")]
                    for msg in (ack, bye):
                        self.assertEqual(msg.split()[1], f"sip:answered@127.0.0.1:{ata.port}")
                        self.assertEqual(server.get_header(msg, "Call-ID"), server.get_header(invite, "Call-ID"))
                        self.assertNotEqual(server.get_header(msg, "Via"), server.get_header(invite, "Via"))
                    seq = int(server.get_header(invite, "CSeq").split()[0])
                    self.assertEqual(server.get_header(ack, "CSeq"), f"{seq} ACK")
                    self.assertEqual(server.get_header(bye, "CSeq"), f"{seq + 1} BYE")

    def test_answered_contact_uses_its_own_port(self):
        with FakeATA() as contact:
            def answer(ata, msg, peer):
                if msg.startswith("INVITE "):
                    ata.response(msg, peer, "200 OK",
                                 extra=f"Contact: <sip:answered@127.0.0.1:{contact.port}>\r\n")
            with FakeATA(answer) as ata:
                with self.assertRaisesRegex(RuntimeError, "answered unexpectedly"):
                    self.call(ata).ring(1)
                wait_until(lambda: "BYE" in contact.methods())
                self.assertEqual(contact.methods(), ["ACK", "BYE"])
                self.assertEqual(ata.methods(), ["INVITE"])


class ControllerTests(unittest.TestCase):
    def test_stop_while_ringing(self):
        with FakeATA() as ata, configured(ata, RING_SECONDS=20) as controller:
            controller.start()
            wait_until(lambda: "INVITE" in ata.methods())
            started = time.monotonic()
            controller.stop()
            controller.thread.join(1.5)
            self.assertFalse(controller.thread.is_alive())
            self.assertLess(time.monotonic() - started, 1.5)
            wait_until(lambda: "ACK" in ata.methods())
            self.assertEqual(ata.methods().count("INVITE"), 1)

    def test_stop_during_retrigger_wait(self):
        with FakeATA() as ata, configured(ata) as controller:
            controller.start()
            wait_until(lambda: controller.status()["last_success_at"] is not None)
            controller.stop()
            controller.thread.join(0.5)
            self.assertFalse(controller.thread.is_alive())
            self.assertEqual(ata.methods().count("INVITE"), 1)

    def test_failsafe_interrupts_ringing_wait_and_retry(self):
        for mode in ("ringing", "wait", "retry"):
            with self.subTest(mode=mode):
                def behavior(ata, msg, peer):
                    if mode == "retry" and msg.startswith("INVITE "):
                        ata.response(msg, peer, "503 Unavailable")
                    else:
                        FakeATA.normal(ata, msg, peer)
                with FakeATA(behavior) as ata, configured(
                    ata, MAX_ALARM_SECONDS=0.3,
                    RING_SECONDS=20 if mode == "ringing" else 0.05,
                    ERROR_RETRY_SECONDS=30,
                ) as controller:
                    controller.start()
                    controller.thread.join(1.5)
                    self.assertFalse(controller.thread.is_alive())
                    self.assertEqual(ata.methods().count("INVITE"), 1)
                    self.assertFalse(controller.status()["active"])

    def test_retry_recovers(self):
        def transient(ata, msg, peer):
            if msg.startswith("INVITE ") and ata.methods().count("INVITE") == 1:
                ata.response(msg, peer, "503 Unavailable")
            else:
                FakeATA.normal(ata, msg, peer)
        with FakeATA(transient) as ata, configured(ata) as controller:
            controller.start()
            wait_until(lambda: controller.status()["last_success_at"] is not None)
            self.assertEqual(controller.status()["cycles_sent"], 2)
            self.assertIsNone(controller.status()["last_error"])

    def test_concurrent_start_creates_one_worker(self):
        with FakeATA() as ata, configured(ata) as controller:
            barrier = threading.Barrier(12)
            results = []
            def start():
                barrier.wait()
                results.append(controller.start())
            threads = [threading.Thread(target=start) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            self.assertEqual(results.count((True, "started")), 1)
            self.assertEqual(results.count((False, "already_active")), 11)

    def test_shutdown_disallows_new_starts(self):
        with FakeATA() as ata, configured(ata) as controller:
            controller.start()
            controller.shutdown()
            self.assertEqual(controller.start(), (False, "shutting_down"))

    def test_restart_resets_run_and_old_failsafe(self):
        with FakeATA() as ata, configured(ata, MAX_ALARM_SECONDS=0.3) as controller:
            controller.start()
            wait_until(lambda: controller.status()["last_success_at"] is not None)
            old_id = controller.status()["run_id"]
            controller.stop()
            controller.thread.join(1)
            with patch.object(server, "MAX_ALARM_SECONDS", 0):
                self.assertEqual(controller.start(), (True, "started"))
                wait_until(lambda: controller.status()["last_success_at"] is not None)
                time.sleep(0.4)
                self.assertTrue(controller.status()["active"])
                self.assertNotEqual(controller.status()["run_id"], old_id)
                self.assertEqual(controller.status()["cycles_sent"], 1)


class HttpTests(unittest.TestCase):
    def request(self, httpd, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    @contextlib.contextmanager
    def http_server(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            yield httpd
        finally:
            httpd.shutdown()
            thread.join(2)
            httpd.server_close()

    def test_start_stop_idempotency(self):
        with FakeATA() as ata, configured(ata) as controller, self.http_server() as httpd:
            status, first = self.request(httpd, "POST", "/alarm/start")
            self.assertEqual(status, 202)
            status, second = self.request(httpd, "POST", "/alarm/start")
            self.assertEqual((status, second["result"]), (200, "already_active"))
            self.assertEqual(first["run_id"], second["run_id"])
            for _ in range(2):
                self.assertEqual(self.request(httpd, "POST", "/alarm/stop")[0], 200)
            controller.thread.join(2)
            status, stopped = self.request(httpd, "POST", "/alarm/stop")
            self.assertEqual((status, stopped["result"]), (200, "already_stopped"))
            self.assertFalse(stopped["active"])
            timestamp = stopped["stopped_at"]
            self.assertEqual(self.request(httpd, "POST", "/alarm/stop")[1]["stopped_at"], timestamp)
            self.assertEqual(self.request(httpd, "GET", "/status")[0], 200)

    def test_auth_and_health(self):
        with FakeATA() as ata, configured(ata, API_TOKEN="test-token"), self.http_server() as httpd:
            self.assertEqual(self.request(httpd, "GET", "/health"), (200, {"ok": True}))
            for method, path in (("GET", "/status"), ("POST", "/alarm/start"), ("POST", "/alarm/stop")):
                self.assertEqual(self.request(httpd, method, path)[0], 401)
            for headers in ({"Authorization": "Bearer test-token"}, {"X-API-Key": "test-token"}):
                self.assertEqual(self.request(httpd, "GET", "/status", headers)[0], 200)
                self.assertEqual(self.request(httpd, "POST", "/alarm/stop", headers)[0], 200)
            self.assertEqual(self.request(httpd, "GET", "/status", {"X-API-Key": "wrong"})[0], 401)
            self.assertEqual(self.request(httpd, "GET", "/missing", {"X-API-Key": "test-token"})[0], 404)

    def test_start_while_stopping_returns_conflict(self):
        release_cancel = threading.Event()
        def slow_cancel(ata, msg, peer):
            if msg.startswith("CANCEL "):
                release_cancel.wait(2)
            FakeATA.normal(ata, msg, peer)
        with FakeATA(slow_cancel) as ata, configured(ata, RING_SECONDS=20) as controller, self.http_server() as httpd:
            self.request(httpd, "POST", "/alarm/start")
            wait_until(lambda: "INVITE" in ata.methods())
            self.request(httpd, "POST", "/alarm/stop")
            wait_until(lambda: "CANCEL" in ata.methods())
            try:
                status, result = self.request(httpd, "POST", "/alarm/start")
                self.assertEqual((status, result["result"]), (409, "still_stopping"))
            finally:
                release_cancel.set()
            controller.thread.join(2)
            self.assertEqual(ata.methods().count("INVITE"), 1)

    @unittest.skipUnless(hasattr(signal, "SIGTERM"), "POSIX shutdown test")
    def test_sigterm_while_ringing(self):
        with FakeATA() as ata:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            env = {**os.environ, "ATA_HOST": "127.0.0.1", "ATA_PORT": str(ata.port),
                   "HTTP_BIND": "127.0.0.1", "HTTP_PORT": str(port), "API_TOKEN": "",
                   "RING_SECONDS": "20", "RETRIGGER_INTERVAL": "32",
                   "MAX_ALARM_SECONDS": "0", "PYTHONDONTWRITEBYTECODE": "1"}
            process = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                def ready():
                    if process.poll() is not None:
                        raise AssertionError(process.stdout.read())
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            return True
                    except OSError:
                        return False
                # Interpreter startup under ARM emulation can take several
                # seconds. The SIGTERM cleanup deadline below stays strict.
                wait_until(ready, timeout=15)
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("POST", "/alarm/start")
                response = conn.getresponse()
                self.assertEqual(response.status, 202)
                response.read()
                conn.close()
                wait_until(lambda: "INVITE" in ata.methods())
                time.sleep(0.1)
                process.send_signal(signal.SIGTERM)
                output, _ = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 0, output)
                wait_until(lambda: "ACK" in ata.methods())
                self.assertIn("CANCEL", ata.methods())
                self.assertEqual(ata.methods().count("INVITE"), 1)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()


class ConfigTests(unittest.TestCase):
    def test_environment_aliases_and_default_port(self):
        for extra, expected in (({"SPA_HOST": "192.0.2.10", "SPA_PORT": "5062", "SPA_USER": "legacy"},
                                 ["192.0.2.10", 5062, "legacy", 18080]),
                                ({"SPA_HOST": "192.0.2.10", "ATA_HOST": "192.0.2.20",
                                  "SPA_PORT": "5062", "ATA_PORT": "5063", "ATA_USER": "new"},
                                 ["192.0.2.20", 5063, "new", 18080])):
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ATA_", "SPA_")) and k != "HTTP_PORT"}
            env.update(extra)
            result = subprocess.run([sys.executable, "-c", "import json,server; print(json.dumps([server.ATA_HOST,server.ATA_PORT,server.ATA_USER,server.HTTP_PORT]))"],
                                    cwd=ROOT, env=env, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), expected)

    def test_invalid_configuration(self):
        for setting in ({"ATA_HOST": ""}, {"ATA_USER": "bad\r\nHeader: yes"},
                        {"ATA_PORT": 0}, {"HTTP_PORT": 65536},
                        {"RING_SECONDS": float("nan")}, {"MAX_ALARM_SECONDS": -1},
                        {"MAX_ALARM_SECONDS": float("inf")}, {"ERROR_RETRY_SECONDS": 0},
                        {"RETRIGGER_INTERVAL": 1}):
            with self.subTest(setting=setting), patch.multiple(server, ATA_HOST="192.0.2.10"):
                with patch.multiple(server, **setting), self.assertRaises(ValueError):
                    server.validate_config()


if __name__ == "__main__":
    unittest.main()
