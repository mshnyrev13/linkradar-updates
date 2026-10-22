import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def sample(ts, latency=None, status="ok"):
    return {"ts": ts, "latency": latency, "status": status, "ip": "127.0.0.1", "detail": ""}


class MathTests(unittest.TestCase):
    def test_loss_denominator_errors_and_jitter(self):
        result = server.metrics([sample(0, 10), sample(1, 30), sample(2, status="timeout"),
                                 sample(3, 90), sample(4, status="error"), sample(5, 5)])
        self.assertEqual(result["loss"], 20)
        self.assertEqual(result["errors"], 1)
        self.assertIsNone(result["jitter"])  # last successful sample starts a new sequence
        self.assertEqual(result["mean_delta"], 20)
        self.assertEqual(result["avg"], 33.75)
        self.assertEqual(result["p95"], 90)

    def test_accumulator_matches_reference(self):
        data = [sample(i, (i * 13) % 42, "ok") if i % 7 else sample(i, status="timeout") for i in range(100)]
        data[20] = sample(20, status="error")
        accumulator = server.Accumulator()
        for s in data:
            accumulator.add(s)
        result = accumulator.stats(0, 99)
        self.assertEqual(result, server.metrics(data))

    def test_no_green_for_short_or_gapped_capture(self):
        config = server.validate_config({"duration_minutes": 1, "targets": [{"host": "127.0.0.1"}]})
        accumulator = server.Accumulator()
        for i in range(20):
            accumulator.add(sample(i, 1))
        self.assertEqual(server.assessment(accumulator.stats(0, 20), config, 20)["status"], "pending")
        # A missing tail remains insufficient, even if latency was always good.
        self.assertEqual(server.assessment(accumulator.stats(0, 60), config, 60)["status"], "pending")
        accumulator = server.Accumulator()
        for i in range(60):
            accumulator.add(sample(i, 1))
        self.assertEqual(server.assessment(accumulator.stats(0, 60), config, 60)["status"], "stable")

    def test_burst_not_hidden_by_low_average_loss(self):
        config = server.validate_config({"duration_minutes": 1, "loss_limit": 10, "burst_limit": 2,
                                         "targets": [{"host": "127.0.0.1"}]})
        data = [sample(i, 1) for i in range(60)]
        for i in (20, 21, 22):
            data[i] = sample(i, status="timeout")
        self.assertEqual(server.assessment(server.metrics(data), config, 60)["status"], "risk")


class OSParsingTests(unittest.TestCase):
    def test_ping_unix_and_localized_windows(self):
        cases = [("64 bytes: time=0.042 ms", .042), ("Reply: time<1ms TTL=128", 1),
                 ("Ответ от 127.0.0.1: число байт=32 время=12мс TTL=128", 12),
                 ("Antwort: Zeit=1ms TTL=128", 1), ("temps=1,3 ms", 1.3)]
        for raw, value in cases:
            with self.subTest(raw=raw):
                self.assertEqual(server.parse_ping(raw, 0)["latency"], value)

    def test_permission_denied_not_packet_loss(self):
        self.assertEqual(server.parse_ping("ping: sendto: Operation not permitted", 2)["status"], "error")
        self.assertEqual(server.parse_ping("100% packet loss", 1)["status"], "timeout")
        self.assertEqual(server.parse_ping("Unknown localized successful response", 0)["status"], "error")

    def test_os_commands(self):
        self.assertIn("-w", server.ping_command("127.0.0.1", 1, "Windows"))
        command = server.ping_command("127.0.0.1", 1, "Darwin")
        self.assertEqual(command[-2], "1000")

    def test_route_parsers(self):
        raw = " 1 192.168.1.1 1.23 ms\n 2 *\n 3 1.1.1.1 8.90 ms\n"
        route = server.parse_route(raw)
        self.assertEqual(route[1]["ip"], None)
        self.assertEqual(route[2]["latency"], 8.9)
        windows = " 1 <1 мс <1 мс <1 мс 192.168.1.1\n 2 * * * Превышен интервал ожидания\n"
        self.assertEqual(server.parse_route(windows)[0]["ip"], "192.168.1.1")


class ValidationTests(unittest.TestCase):
    def test_no_shell_syntax_or_urls(self):
        for host in ("-h", "a;echo hacked", "https://example.com/key", "a\nwhoami", "x$(id)", "a:443"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                server.host_value(host)
        self.assertEqual(server.host_value("::1"), "::1")
        self.assertEqual(server.host_value("пример.рф"), "xn--e1afmkfd.xn--p1ai")

    def test_config_rejects_nonfinite_and_bad_limits(self):
        for update in ({"interval": "nan"}, {"timeout": 2, "interval": 1}, {"targets": []},
                       {"targets": [{"host": "localhost", "required": False}]}):
            data = {"targets": [{"host": "localhost"}], **update}
            with self.assertRaises(ValueError):
                server.validate_config(data)


class StorageTests(unittest.TestCase):
    def test_restart_preserves_data_and_marks_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "db.sqlite3"
            store = server.Store(path)
            config = server.validate_config({"targets": [{"host": "127.0.0.1"}]})
            store.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", ("s", 1, None, "running", json.dumps(config)))
            store.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?)", ("s", config["targets"][0]["id"], 2, "ok", 1, "127.0.0.1", ""))
            store.db.close()
            reopened = server.Store(path)
            self.assertEqual(reopened.rows("SELECT status FROM sessions")[0]["status"], "interrupted")
            self.assertEqual(len(reopened.rows("SELECT * FROM samples")), 1)
            reopened.db.close()

    def test_csv_formula_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            store = server.Store(Path(folder) / "db.sqlite3")
            config = server.validate_config({"targets": [{"host": "localhost", "label": "=HYPERLINK(1)"}]})
            store.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", ("s", 1, 2, "completed", json.dumps(config)))
            store.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?)", ("s", config["targets"][0]["id"], 1, "ok", 1, "127.0.0.1", ""))
            self.assertIn("'=HYPERLINK", server.csv_export(store, "s").decode("utf-8"))
            store.db.close()

    def test_worker_high_latency_recovers_without_crashing(self):
        with tempfile.TemporaryDirectory() as folder:
            store = server.Store(Path(folder) / "db.sqlite3")
            monitor = server.Monitor(store)
            config = server.validate_config({"targets": [{"host": "127.0.0.1"}]})
            config["interval"] = .001
            t = config["targets"][0]
            monitor.accumulators[("s", t["id"])] = server.Accumulator()
            stop = threading.Event()
            results = iter([200, 210, 10])

            def fake_probe(*args):
                try:
                    return sample(time.time(), next(results))
                except StopIteration:
                    stop.set()
                    return sample(time.time(), 10)
            with patch.object(server, "probe", fake_probe):
                monitor.worker("s", t, config, time.time(), stop)
            events = store.rows("SELECT * FROM events")
            self.assertEqual(len(events), 2)
            self.assertIn("вернулась", events[1]["message"])
            store.db.close()


if __name__ == "__main__":
    unittest.main()
