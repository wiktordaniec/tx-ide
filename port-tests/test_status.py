"""STATUS — `claude/statusline.sh` (spec T-STATUS-01..10).

The script is run directly with `bash`; stdout is compared RAW because the ANSI escapes are the
contract. The reset countdown is computed with `date +%s` at run time, so `resets_at` values are
built from the wall clock with ~60 s of slack inside the same hour bucket.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from txkit import TX_STATUSLINE, GitFixture, TxCase, scrubbed_env

# Entry point (H9 / D16): the reference's `claude/statusline.sh` by default, a port's own seam otherwise.
STATUSLINE = Path(TX_STATUSLINE)

DIM = "\x1b[38;2;169;177;214m"
DIR = "\x1b[38;5;31m"
B = "\x1b[1m"
R = "\x1b[0m"

CURL_TIMEOUT = 0.3  # `curl -m 0.3` in push_anthropic_usage


@dataclass
class RecordedRequest:
    method: str
    path: str
    content_type: str
    body: str
    received_at: float


class UsageListener:
    """A local HTTP listener that records every request. With `hold=True` the handler blocks after
    recording until `release()` (or `close()`), never answering — the "listener hangs" shape.
    `accepted` counts every connection the server accepted (a bare connect or a GET too), so
    "no connection attempted" is a real check rather than "no completed POST recorded"."""

    def __init__(self, hold: bool = False):
        self.hold = hold
        self.requests: list[RecordedRequest] = []
        self.accepted = 0
        self.release_event = threading.Event()
        self.completed_event = threading.Event()
        listener = self

        class Server(HTTPServer):
            def verify_request(self, request, client_address) -> bool:
                listener.accepted += 1
                return True

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["content-length"])).decode()
                listener.requests.append(
                    RecordedRequest(
                        method=self.command,
                        path=self.path,
                        content_type=self.headers["content-type"],
                        body=body,
                        received_at=time.monotonic(),
                    )
                )
                if listener.hold:
                    listener.release_event.wait()
                else:
                    self.send_response(200)
                    self.end_headers()
                listener.completed_event.set()

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def release(self) -> None:
        self.release_event.set()

    def close(self) -> None:
        self.release_event.set()
        self.server.shutdown()
        self.server.server_close()


class TestStatus(TxCase):
    def run_statusline(
        self,
        payload: dict | str,
        *,
        cwd: Path | None = None,
        env: dict[str, str | None] | None = None,
        timeout: float = 30.0,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(STATUSLINE)],
            input=payload if isinstance(payload, str) else json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=scrubbed_env(self.home, extra=env),
            cwd=str(cwd or self.root),
            timeout=timeout,
        )

    def listener(self, hold: bool = False) -> UsageListener:
        listener = UsageListener(hold=hold)
        self.addCleanup(listener.close)
        return listener

    def write_port_file(self, port: int | str, path: Path | None = None) -> None:
        port_file = path or (self.home.path / "sessions-graph.port")
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.write_text(str(port))

    def wait_for_request(self, listener: UsageListener, timeout: float = 3.0) -> RecordedRequest:
        self.wait_until(lambda: listener.requests, timeout=timeout, interval=0.02)
        return listener.requests[0]

    def curl_processes(self, port: int) -> list[int]:
        """Pids of the live `curl` processes whose argv names this listener's endpoint (/proc)."""
        needle = f"http://127.0.0.1:{port}/api/anthropic-usage".encode()
        pids = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if cmdline.split(b"\0", 1)[0].endswith(b"curl") and needle in cmdline:
                pids.append(int(entry.name))
        return pids

    def assert_no_connection(self, listener: UsageListener, port: int | None = None) -> None:
        """Nothing reached the listener, and no curl aimed at it is (still) around."""
        time.sleep(0.5)
        self.assertEqual(listener.accepted, 0)
        self.assertEqual(listener.requests, [])
        self.assertEqual(self.curl_processes(port or listener.port), [])

    # --- T-STATUS-01 -------------------------------------------------------------------------

    def test_t_status_01_full_payload_two_lines(self):
        repo = GitFixture(self.root, "myrepo")
        (repo.path / "src").mkdir()
        now = int(time.time())
        payload = {
            "cwd": str(repo.path / "src"),
            "model": {"display_name": "Claude Sonnet 4.6 (1M context)"},
            "effort": {"level": "high"},
            "context_window": {
                "current_usage": {
                    "input_tokens": 1000,
                    "cache_creation_input_tokens": 200000,
                    "cache_read_input_tokens": 1049000,
                }
            },
            "rate_limits": {
                "five_hour": {"used_percentage": 3.2, "resets_at": now + 100},
                "seven_day": {"used_percentage": 41.6, "resets_at": now + 90000 + 60},
            },
        }
        result = self.run_statusline(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            f"{B}{DIR}myrepo{R}\n"
            f"{DIM}Sonnet 4.6{R} {DIM}effort:{B}3{R} {B}{DIM}1.2M{R} {DIM}7d:{B}42%{R} {DIM}1d1h↺{R}",
        )

    # --- T-STATUS-02 -------------------------------------------------------------------------

    def test_t_status_02_fields_read_exact_paths(self):
        payload = {
            "unknown_top_level": {"cwd": "/nowhere", "model": {"display_name": "Claude Nope"}},
            "session_id": "abc",
            "cwd": str(self.root / "somewhere"),
            "workspace": {"current_dir": "/elsewhere"},
            "model": {"id": "claude-opus-4-1", "display_name": "Claude Opus 4.1", "vendor": "anthropic"},
            "effort": {"level": "medium", "label": "high"},
            "context_window": {
                "total_input_tokens": 999999,
                "total_tokens": 999999,
                "current_usage": {
                    "input_tokens": 1000,
                    "cache_read_input_tokens": 11345,
                    "output_tokens": 500000,
                },
            },
            "total_input_tokens": 999999,
            "rate_limits": {
                "seven_day": {"used_percentage": 20.4, "limit": 100, "remaining_percentage": 5.0},
                "one_hour": {"used_percentage": 99.9, "resets_at": 1},
            },
        }
        result = self.run_statusline(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            f"{B}{DIR}somewhere{R}\n{DIM}Opus 4.1{R} {DIM}effort:{B}2{R} {B}{DIM}12.3K{R} {DIM}7d:{B}20%{R}",
        )

    # --- T-STATUS-03 -------------------------------------------------------------------------

    def test_t_status_03_line1_git_toplevel_vs_cwd_basename(self):
        model = {"display_name": "Sonnet"}
        line2 = f"{DIM}Sonnet{R}"

        inside_repo = self.git.path / "src"
        inside_repo.mkdir()
        result = self.run_statusline({"cwd": str(inside_repo), "model": model})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{B}{DIR}repo{R}\n{line2}")

        plain = self.root / "plain" / "dir"
        plain.mkdir(parents=True)
        toplevel = subprocess.run(
            ["git", "-C", str(plain), "rev-parse", "--show-toplevel"], capture_output=True, text=True
        )
        self.assertNotEqual(toplevel.returncode, 0, f"{plain} is unexpectedly inside a git repo")
        result = self.run_statusline({"cwd": str(plain), "model": model})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{B}{DIR}dir{R}\n{line2}")

        result = self.run_statusline({"model": model})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, line2)

    def test_t_status_03_linked_worktree_uses_its_own_basename(self):
        # A subdir of the worktree: `basename $cwd` would say `src`, the main checkout's toplevel
        # would say `repo`; only `--show-toplevel` of the linked worktree gives `repo-linked`.
        linked = self.git.as_linked_worktree()
        inside = linked / "src"
        inside.mkdir()
        result = self.run_statusline({"cwd": str(inside), "model": {"display_name": "Sonnet"}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{B}{DIR}repo-linked{R}\n{DIM}Sonnet{R}")

    # --- T-STATUS-04 -------------------------------------------------------------------------

    def test_t_status_04_model_name_normalisation(self):
        cases = {
            "Claude 3.5 Sonnet": "3.5 Sonnet",
            "Claude Opus 4.1 (1M context)": "Opus 4.1",
            "Sonnet": "Sonnet",
            "Claude X (a) (b)": "X (b)",
        }
        for display_name, expected in cases.items():
            with self.subTest(display_name=display_name):
                result = self.run_statusline({"model": {"display_name": display_name}})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"{DIM}{expected}{R}")

    def test_t_status_04_display_name_absent_no_model_segment(self):
        result = self.run_statusline({"model": {"id": "claude-sonnet-4-6"}, "effort": {"level": "low"}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{DIM}effort:{B}1{R}")

    # --- T-STATUS-05 -------------------------------------------------------------------------

    def test_t_status_05_effort_mapping(self):
        cases = {"low": "1", "medium": "2", "high": "3", "xhigh": "4", "max": "5", "turbo": "turbo"}
        for level, expected in cases.items():
            with self.subTest(level=level):
                result = self.run_statusline({"effort": {"level": level}})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"{DIM}effort:{B}{expected}{R}")
        for label, payload in (
            ("level absent", {"model": {"display_name": "Sonnet"}, "effort": {}}),
            ("effort key absent", {"model": {"display_name": "Sonnet"}}),
        ):
            with self.subTest(level=label):
                result = self.run_statusline(payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"{DIM}Sonnet{R}")

    # --- T-STATUS-06 -------------------------------------------------------------------------

    def test_t_status_06_token_formatting(self):
        cases = {
            0: None,
            999: "999",
            1000: "1.0K",
            12345: "12.3K",
            999950: "1000.0K",
            1000000: "1.0M",
            1234567: "1.2M",
        }
        for total, expected in cases.items():
            with self.subTest(total=total):
                payload = {"context_window": {"current_usage": {"input_tokens": total}}}
                result = self.run_statusline(payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "" if expected is None else f"{B}{DIM}{expected}{R}")
        with self.subTest(total="split across the three fields"):
            payload = {
                "context_window": {
                    "current_usage": {
                        "input_tokens": 345,
                        "cache_creation_input_tokens": 2000,
                        "cache_read_input_tokens": 10000,
                    }
                }
            }
            result = self.run_statusline(payload)
            self.assertEqual(result.stdout, f"{B}{DIM}12.3K{R}")

    def test_t_status_06_all_usage_fields_absent_no_segment(self):
        payload = {
            "model": {"display_name": "Sonnet"},
            "context_window": {"current_usage": {"output_tokens": 5000}},
        }
        result = self.run_statusline(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{DIM}Sonnet{R}")

    # --- T-STATUS-07 -------------------------------------------------------------------------

    def test_t_status_07_seven_day_rate_limit_with_reset_countdown(self):
        now = int(time.time())
        payload = {
            "rate_limits": {"seven_day": {"used_percentage": 12.4, "resets_at": now + 3 * 86400 + 2 * 3600 + 59}}
        }
        result = self.run_statusline(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{DIM}7d:{B}12%{R} {DIM}3d2h↺{R}")

    def test_t_status_07_resets_at_absent_or_null_no_suffix(self):
        for seven_day in ({"used_percentage": 12.4}, {"used_percentage": 12.4, "resets_at": None}):
            with self.subTest(seven_day=seven_day):
                result = self.run_statusline({"rate_limits": {"seven_day": seven_day}})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"{DIM}7d:{B}12%{R}")

    def test_t_status_07_resets_at_in_the_past(self):
        now = int(time.time())
        payload = {"rate_limits": {"seven_day": {"used_percentage": 12.4, "resets_at": now - 5000}}}
        result = self.run_statusline(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{DIM}7d:{B}12%{R} {DIM}0d0h↺{R}")

    def test_t_status_07_used_percentage_rounding_and_absence(self):
        result = self.run_statusline({"rate_limits": {"seven_day": {"used_percentage": 12.6}}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{DIM}7d:{B}13%{R}")

        now = int(time.time())
        payload = {"model": {"display_name": "Sonnet"}, "rate_limits": {"seven_day": {"resets_at": now + 90060}}}
        result = self.run_statusline(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{DIM}Sonnet{R}")

    def test_t_status_07_five_hour_only_prints_nothing_about_it(self):
        now = int(time.time())
        payload = {
            "model": {"display_name": "Sonnet"},
            "rate_limits": {"five_hour": {"used_percentage": 87.5, "resets_at": now + 100}},
        }
        result = self.run_statusline(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{DIM}Sonnet{R}")

    # --- T-STATUS-08 -------------------------------------------------------------------------

    def test_t_status_08_segment_order_and_joining(self):
        payload = {"effort": {"level": "medium"}, "rate_limits": {"seven_day": {"used_percentage": 5}}}
        result = self.run_statusline(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{DIM}effort:{B}2{R} {DIM}7d:{B}5%{R}")

    # --- T-STATUS-09 -------------------------------------------------------------------------

    def test_t_status_09_empty_payload(self):
        result = self.run_statusline({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_t_status_09_invalid_json(self):
        result = self.run_statusline("not json\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotEqual(result.stderr, "")
        for line in result.stderr.splitlines():
            self.assertTrue(line.startswith("jq: "), line)

    # --- T-STATUS-10 -------------------------------------------------------------------------

    def usage_payload(self) -> tuple[dict, str]:
        now = int(time.time())
        resets_5h = now + 100
        resets_7d = now + 90000 + 60
        payload = {
            "model": {"display_name": "Sonnet"},
            "rate_limits": {
                "five_hour": {"used_percentage": 3.2, "resets_at": resets_5h},
                "seven_day": {"used_percentage": 41.6, "resets_at": resets_7d},
            },
        }
        body = (
            f'{{"five_hour":{{"used_percentage":3.2,"resets_at":{resets_5h}}},'
            f'"seven_day":{{"used_percentage":41.6,"resets_at":{resets_7d}}}}}'
        )
        return payload, body

    def test_t_status_10_background_post_to_sessions_graph(self):
        listener = self.listener(hold=True)
        self.write_port_file(listener.port)
        payload, body = self.usage_payload()

        process = subprocess.Popen(
            ["bash", str(STATUSLINE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=scrubbed_env(self.home),
            cwd=str(self.root),
        )
        stdout, stderr = process.communicate(json.dumps(payload), timeout=30)
        # stdout EOF seen. The held listener never answers, so a curl that has connected stays alive
        # until its own `-m 0.3`; a script that kept the pipe open until curl gave up would hand us
        # EOF only AFTER that — with the request long recorded and curl gone.
        request_pending = not listener.requests or self.curl_processes(listener.port)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, f"{DIM}Sonnet{R} {DIM}7d:{B}42%{R} {DIM}1d1h↺{R}")
        self.assertTrue(request_pending, "the POST had already completed when the script's stdout closed")

        request = self.wait_for_request(listener)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.path, "/api/anthropic-usage")
        self.assertEqual(request.content_type, "application/json")
        self.assertEqual(request.body, body)
        self.assertEqual(len(listener.requests), 1)
        self.assertEqual(listener.accepted, 1)

        listener.release()
        self.assertTrue(listener.completed_event.wait(3.0))
        # positive control on the /proc probe: once answered (or timed out) the curl is gone
        self.wait_until(lambda: not self.curl_processes(listener.port), timeout=3.0)

    def test_t_status_10_missing_limit_posts_null(self):
        listener = self.listener()
        self.write_port_file(listener.port)
        now = int(time.time())
        payload = {"rate_limits": {"seven_day": {"used_percentage": 41.6, "resets_at": now + 90060}}}
        result = self.run_statusline(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        request = self.wait_for_request(listener)
        self.assertEqual(
            request.body,
            f'{{"five_hour":{{"used_percentage":null,"resets_at":null}},'
            f'"seven_day":{{"used_percentage":41.6,"resets_at":{now + 90060}}}}}',
        )

    def test_t_status_10_no_connection_without_port_or_rate_limits(self):
        payload, _ = self.usage_payload()
        port_file = self.home.path / "sessions-graph.port"

        with self.subTest(edge="port file absent"):
            listener = self.listener()
            self.assertFalse(port_file.exists())
            result = self.run_statusline(payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_no_connection(listener)

        with self.subTest(edge="port file empty"):
            listener = self.listener()
            port_file.write_text("")
            result = self.run_statusline(payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_no_connection(listener)

        with self.subTest(edge="no rate_limits"):
            listener = self.listener()
            port_file.write_text(str(listener.port))
            result = self.run_statusline({"model": {"display_name": "Sonnet"}, "effort": {"level": "high"}})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"{DIM}Sonnet{R} {DIM}effort:{B}3{R}")
            self.assert_no_connection(listener)

        with self.subTest(edge="positive control"):
            # the same listener shape, port file and wait DO see a connection once rate_limits exist
            listener = self.listener()
            port_file.write_text(str(listener.port))
            result = self.run_statusline(payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.wait_for_request(listener)
            self.assertEqual(listener.accepted, 1)

    def test_t_status_10_listener_hangs_script_returns_promptly(self):
        listener = self.listener(hold=True)
        self.write_port_file(listener.port)
        payload, body = self.usage_payload()
        started_at = time.monotonic()
        result = self.run_statusline(payload, timeout=5.0)
        elapsed = time.monotonic() - started_at
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{DIM}Sonnet{R} {DIM}7d:{B}42%{R} {DIM}1d1h↺{R}")
        self.assertLess(elapsed, 2.0)
        request = self.wait_for_request(listener)
        self.assertEqual(request.body, body)
        self.assertEqual(listener.accepted, 1)
        # The handler never answers; curl gives up on its own (-m 0.3) and the script is long gone.
        self.wait_until(lambda: not self.curl_processes(listener.port), timeout=3.0)
        self.assertFalse(listener.completed_event.is_set())

    def test_t_status_10_tx_ide_home_unset_uses_home_dot_tx_ide(self):
        listener = self.listener()
        self.write_port_file(listener.port, self.home.user_home / ".tx-ide" / "sessions-graph.port")
        payload, body = self.usage_payload()
        result = self.run_statusline(payload, env={"TX_IDE_HOME": None})
        self.assertEqual(result.returncode, 0, result.stderr)
        request = self.wait_for_request(listener)
        self.assertEqual(request.path, "/api/anthropic-usage")
        self.assertEqual(request.body, body)
