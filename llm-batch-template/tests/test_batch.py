import asyncio
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from llm_batch.__main__ import main
from llm_batch.control import RateLimiter, retry_after_seconds
from llm_batch.pipeline import Settings, run
from llm_batch.provider import FatalProviderError, OpenAICompatible, RetryableError, RowError
from llm_batch.store import AsyncStore, Store
from llm_batch.task import load_task


ROOT = Path(__file__).resolve().parents[1]


class CommandTests(unittest.TestCase):
    def setUp(self):
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.root = Path(data.name)
        self.source = self.root / "input.jsonl"
        self.db = self.root / "batch.sqlite"
        self.output = self.root / "result.jsonl"
        self.command = [sys.executable, "-m", "llm_batch"]

    def batch(self, source=None, *options, rpm="6000"):
        return self.command + ["run", "--input", str(source or self.source), "--db", str(self.db), "--rpm", rpm, *options]

    def invoke(self, command):
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=12)

    def rows(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute("SELECT id, status, error, attempts FROM jobs ORDER BY seq").fetchall()

    def test_retry_timeout_resume_and_ordered_export(self):
        options = ("--max-attempts", "2", "--request-timeout", "0.1", "--job-timeout", "4")
        command = self.batch(ROOT / "examples/failures.jsonl", *options)
        result = self.invoke(command)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.rows(), [
            ("ok", "succeeded", None, 1), ("bad-output", "failed", "invalid_output_schema", 1),
            ("rate-limited", "failed", "http_429", 2), ("slow", "failed", "request_deadline", 2),
        ])
        resumed = self.invoke(command)
        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        self.assertIn("attempts=0", resumed.stderr)
        retried = self.invoke(command + ["--retry-failed"])
        self.assertEqual(retried.returncode, 2, retried.stderr)
        self.assertEqual([row[-1] for row in self.rows()], [1, 2, 4, 4])
        export = self.command + ["export", "--db", str(self.db), "--output", str(self.output)]
        result = self.invoke(export)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in self.output.read_text().splitlines()]
        self.assertEqual([row["id"] for row in records], [row[0] for row in self.rows()])
        self.assertEqual(records[0]["result"], {})
        self.assertEqual(records[1]["error"], "invalid_output_schema")
        self.assertEqual(self.invoke(export).returncode, 1)

    def test_kill_and_resume_skips_committed_rows(self):
        self.source.write_text(''.join(json.dumps({"id": str(i), "text": "Sample"}) + "\n" for i in range(12)))
        process = subprocess.Popen(self.batch(rpm="120"), cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if self.db.exists():
                    try:
                        if sum(status == "succeeded" for _, status, _, _ in self.rows()) >= 2:
                            break
                    except sqlite3.OperationalError:
                        pass
                time.sleep(0.02)
            else:
                self.fail("runner did not commit any work in time")
            locked = self.invoke(self.batch())
            self.assertEqual(locked.returncode, 1)
            self.assertIn("another run owns", locked.stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

        before = self.rows()
        completed = sum(status == "succeeded" for _, status, _, _ in before)
        self.assertGreaterEqual(completed, 2)
        self.assertLess(completed, 12)
        resumed = self.invoke(self.batch())
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn(f"attempts={12 - completed}", resumed.stderr)
        self.assertTrue(all(row[1:] == ("succeeded", None, 1) for row in self.rows()))

    def test_input_validation_and_resume_signature(self):
        self.source.write_text('{"id":"a","text":"ok"}\n{"id":"a","text":"duplicate"}\n')
        rejected = self.invoke(self.batch())
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("duplicate id", rejected.stderr)
        self.assertEqual(self.rows(), [])
        self.source.write_text('{"id":"a","text":"ok"}\n')
        self.assertEqual(self.invoke(self.batch()).returncode, 0)
        changed_settings = self.invoke(self.batch(None, "--max-output-tokens", "8"))
        self.assertIn("input or task settings changed", changed_settings.stderr)
        self.source.write_text('{"id":"a","text":"changed"}\n')
        changed_input = self.invoke(self.batch())
        self.assertEqual(changed_input.returncode, 1)
        self.assertIn("input or task settings changed", changed_input.stderr)

    def test_blank_task_blocks_real_requests_but_mock_runs_without_task(self):
        self.source.write_text('{"id":"a","text":"sample"}\n')
        task_path = self.root / "blank-task.json"
        task_path.write_text('{"prompt":"","labels":[],"output_schema":{}}\n')
        result = self.invoke(self.batch(None, "--provider", "openai-compatible", "--task", str(task_path)))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("fill in prompt and output_schema", result.stderr)
        self.assertFalse(self.db.exists())
        mock = self.invoke(self.batch(None, "--task", str(self.root / "missing.json")))
        self.assertEqual(mock.returncode, 0, mock.stderr)

    def test_blank_connection_is_rejected_before_creating_checkpoint(self):
        self.source.write_text('{"id":"a","text":"sample"}\n')
        config = self.root / "blank-llm.toml"
        config.write_text('protocol = "chat-completions"\nbase_url = ""\nmodel = ""\n'
                          'api_key_env = "LLM_API_KEY"\noutput_limit_field = ""\n')
        result = self.invoke(self.batch(None, "--llm", str(config)))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("base_url must be an HTTP(S) API root", result.stderr)
        self.assertFalse(self.db.exists())


class ControlTests(unittest.TestCase):
    def test_pacing_and_token_window(self):
        with patch("llm_batch.control.monotonic", return_value=100.0) as clock:
            limiter = RateLimiter(rpm=60, tpm=10)
            self.assertEqual(limiter.admit_or_delay(8), 0)
            self.assertEqual(limiter.admit_or_delay(1), 1)
            clock.return_value = 101.0
            self.assertEqual(limiter.admit_or_delay(3), 59)
            self.assertEqual(limiter.admit_or_delay(2), 0)
            clock.return_value = 160.0
            limiter.cooldown(4)
            self.assertEqual(limiter.admit_or_delay(8), 4)
            clock.return_value = 164.0
            self.assertEqual(limiter.admit_or_delay(8), 0)

    def test_retry_after_accepts_seconds_and_http_date(self):
        self.assertEqual(retry_after_seconds("3"), 3)
        self.assertIsNone(retry_after_seconds("-3"))
        self.assertIsNone(retry_after_seconds("not a date"))
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with patch("llm_batch.control.datetime") as clock:
            clock.now.return_value = now
            value = format_datetime(now + timedelta(seconds=20), usegmt=True)
            self.assertEqual(retry_after_seconds(value), 20)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_workers_share_a_concurrency_limit(self):
        in_flight = peak = 0

        async def complete(text):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(0.03)
                return {"value": 1}
            finally:
                in_flight -= 1

        settings = Settings(8, 2, 3, 6000, 0, 128, 2, 1.0, 2.0, 1.0, False)
        with tempfile.TemporaryDirectory() as temp, closing(sqlite3.connect(":memory:", check_same_thread=False)) as conn:
            source = Path(temp) / "rows.jsonl"
            source.write_text(''.join(json.dumps({"id": str(i), "text": "test"}) + "\n" for i in range(18)))
            store = Store(conn)
            store.initialize(source, {"task": "concurrency-test"})
            with ThreadPoolExecutor(max_workers=1) as executor:
                counts = await run(AsyncStore(store, executor), complete, settings)
        self.assertEqual(counts, {"pending": 0, "succeeded": 18, "failed": 0})
        self.assertEqual(peak, 3)


@unittest.skipUnless(
    all(importlib.util.find_spec(module) for module in ("httpx", "jsonschema")),
    "install requirements.txt to test the HTTP adapter",
)
class HTTPAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_and_response_mapping(self):
        import httpx

        result = {"score": 2, "codes": ["A"]}
        responses = iter([
            httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]}),
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(429, json={"error": {"code": "insufficient_quota"}}),
            httpx.Response(401),
            httpx.ReadTimeout("test timeout"),
            httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": 'invalid JSON'}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": '{"score":"2","codes":["A"]}'}}]}),
        ])

        def handler(request):
            self.assertEqual(str(request.url), "https://example.test/v1/chat/completions")
            body = json.loads(request.content)
            self.assertEqual(body["messages"][1]["content"], "sample text")
            self.assertTrue(body["messages"][0]["content"].startswith("Test prompt"))
            self.assertIn('Label vocabulary: ["A"]', body["messages"][0]["content"])
            self.assertIn('"score"', body["messages"][0]["content"])
            self.assertEqual(body["max_completion_tokens"], 128)
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "task.json"
            path.write_text(json.dumps({"prompt": "Test prompt", "labels": ["A"], "output_schema": {
                "type": "object", "required": ["score", "codes"],
                "properties": {"score": {"type": "integer"}, "codes": {"$ref": "#/$defs/codes"}},
                "$defs": {"codes": {"type": "array", "items": {"enum": ["A"]}}},
            }}))
            task = load_task(path)
            self.assertFalse(task.is_valid({"score": 2, "codes": ["B"]}))

        async with httpx.AsyncClient(base_url="https://example.test/v1/", transport=httpx.MockTransport(handler)) as client:
            adapter = OpenAICompatible(client, "test-model", 128, task.system_prompt)
            self.assertEqual(task.decode_result(await adapter.complete("sample text")), result)
            with self.assertRaises(RetryableError) as retry:
                await adapter.complete("sample text")
            self.assertEqual(retry.exception.retry_after, 2)
            with self.assertRaisesRegex(FatalProviderError, "provider_quota_exhausted"):
                await adapter.complete("sample text")
            with self.assertRaisesRegex(FatalProviderError, "provider_setup_http_401"):
                await adapter.complete("sample text")
            with self.assertRaisesRegex(RetryableError, "http_timeout"):
                await adapter.complete("sample text")
            with self.assertRaisesRegex(RowError, "output_truncated"):
                await adapter.complete("sample text")
            for _ in range(2):
                with self.assertRaisesRegex(RowError, "invalid_output_schema"):
                    task.decode_result(await adapter.complete("sample text"))


@unittest.skipUnless(
    all(importlib.util.find_spec(module) for module in ("httpx", "jsonschema")),
    "install requirements.txt to test LLM connection configuration",
)
class ConnectionTests(unittest.TestCase):
    def test_cli_switches_token_field_and_protects_checkpoint(self):
        import httpx

        client_class = httpx.AsyncClient
        calls = []

        def handler(request):
            calls.append((str(request.url), request.headers["Authorization"], json.loads(request.content)))
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"value":7}'}}]})

        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "input.jsonl"
            source.write_text('{"id":"a","text":"sample"}\n')
            task = folder / "task.json"
            task.write_text('{"prompt":"Test prompt","output_schema":{"type":"object","required":["value"]}}\n')
            config = folder / "llm.toml"
            db = folder / "legacy.sqlite"
            connection = ('protocol = "chat-completions"\nbase_url = "https://example.test/gateway/v1/"\n'
                          'model = "test-model"\napi_key_env = "CUSTOM_KEY"\n')

            def invoke(db_path, *options):
                argv = ["llm_batch", "run", "--input", str(source), "--db", str(db_path), "--task", str(task), *options]
                with patch.object(sys, "argv", argv):
                    return main()

            with patch.dict(os.environ, {
                "LLM_BASE_URL": "https://example.test/gateway/v1", "LLM_MODEL": "test-model",
                "LLM_API_KEY": "legacy-test-key", "CUSTOM_KEY": "configured-test-key",
            }, clear=True), patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: client_class(
                **kwargs, transport=httpx.MockTransport(handler)
            )):
                self.assertEqual(invoke(db, "--provider", "openai-compatible"), 0)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][1], "Bearer legacy-test-key")
                self.assertEqual(calls[0][2]["max_completion_tokens"], 128)
                self.assertNotIn("max_tokens", calls[0][2])
                with closing(sqlite3.connect(db)) as conn, conn:
                    signature = json.loads(conn.execute("SELECT signature FROM run_metadata").fetchone()[0])
                    self.assertEqual(signature.pop("protocol"), "chat-completions")
                    self.assertEqual(signature.pop("output_limit_field"), "max_completion_tokens")
                    conn.execute("UPDATE run_metadata SET signature=?", (json.dumps(signature, sort_keys=True),))

                config.write_text(connection + 'output_limit_field = "max_completion_tokens"\n')
                self.assertEqual(invoke(db, "--llm", str(config)), 0)
                self.assertEqual(len(calls), 1)
                config.write_text(connection + 'output_limit_field = "max_tokens"\n')
                with self.assertLogs("llm_batch.__main__", level="ERROR") as logs:
                    self.assertEqual(invoke(db, "--llm", str(config)), 1)
                self.assertIn("input or task settings changed", logs.output[0])
                self.assertEqual(len(calls), 1)

                fresh_db = folder / "configured.sqlite"
                self.assertEqual(invoke(fresh_db, "--llm", str(config)), 0)
                self.assertEqual(len(calls), 2)
                url, authorization, body = calls[1]
                self.assertEqual(url, "https://example.test/gateway/v1/chat/completions")
                self.assertEqual(authorization, "Bearer configured-test-key")
                self.assertEqual(body["max_tokens"], 128)
                self.assertNotIn("max_completion_tokens", body)
                with closing(sqlite3.connect(fresh_db)) as conn:
                    signature = conn.execute("SELECT signature FROM run_metadata").fetchone()[0]
                    self.assertNotIn("configured-test-key", signature)
                    self.assertEqual(json.loads(conn.execute("SELECT result FROM jobs").fetchone()[0]), {"value": 7})
