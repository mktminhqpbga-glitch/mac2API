"""A configured CDP timeout bounds both socket reads and shared-session queueing."""
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cdp import CDP, CDPSession


class Socket:
    def __init__(self):
        self.timeout = 90
        self.responses = []
        self.commands = []

    def settimeout(self, seconds):
        self.timeout = seconds

    def gettimeout(self):
        return self.timeout

    def send(self, body):
        self.commands.append(json.loads(body))

    def recv(self):
        if self.responses:
            return json.dumps(self.responses.pop(0))
        time.sleep(min(self.timeout, 0.5))
        raise websocket.WebSocketTimeoutException()


class CDPDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.socket = Socket()
        with patch("websocket.create_connection", return_value=self.socket):
            self.cdp = CDP("ws://127.0.0.1/fixture")

    def test_read_timeout_and_late_reply(self):
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.cdp.send("Runtime.evaluate", timeout=0.05)
        self.assertLess(time.monotonic() - start, 0.4)
        self.assertEqual(self.socket.timeout, 90)
        self.socket.responses.extend([{"id": 1, "result": {"late": True}},
                                      {"id": 2, "sessionId": "page", "result": {"ok": True}}])
        session = CDPSession(self.cdp, "page")
        self.assertEqual(session.send("Runtime.evaluate", timeout=0.2)["result"], {"ok": True})
        self.assertEqual(self.socket.commands[-1]["sessionId"], "page")

    def test_waiting_for_another_session_counts_against_timeout(self):
        errors = []

        def send():
            try:
                self.cdp.send("Runtime.evaluate", timeout=0.05)
            except Exception as exc:
                errors.append(exc)

        with self.cdp._lock:
            worker = threading.Thread(target=send)
            worker.start()
            worker.join(timeout=0.4)
        worker.join(timeout=1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TimeoutError)
        self.assertEqual(self.socket.commands, [])

    def test_event_pump_restores_timeout(self):
        self.socket.responses.append({"method": "Page.loadEventFired"})
        observed = []
        self.cdp.pump(0.05, observed.append)
        self.assertEqual(observed, [{"method": "Page.loadEventFired"}])
        self.assertEqual(self.socket.timeout, 90)


if __name__ == "__main__":
    unittest.main()
