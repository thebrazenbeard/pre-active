from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from pre_active.engine import NonRetryableModelError, RetryableModelError
from pre_active.model_targets import ModelTarget, probe_model_target
from pre_active.providers.openai_compatible import OpenAICompatibleAdapter


@contextmanager
def local_endpoint(*, status=200, location=None, retry_after=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.reply()

        def do_POST(self):
            self.reply()

        def reply(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"method": self.command, "path": self.path,
                             "body": body, "authorization": self.headers.get("Authorization")})
            payload = {"data": [{"id": "local-test"}]} if self.command == "GET" else {
                "choices": [{"message": {"content": "local response"}}]}
            raw = json.dumps(payload).encode()
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            if retry_after:
                self.send_header("Retry-After", retry_after)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, _format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def target(base_url):
    return ModelTarget(name="local", provider="openai-compatible",
                       base_url=base_url + "/v1", model="local-test", api_key_env=None)


def adapter(base_url):
    return OpenAICompatibleAdapter(base_url=base_url + "/v1", model="local-test",
                                   api_key="host-local-secret", timeout_seconds=2)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("operation", ["probe", "inference"])
def test_local_requests_reject_redirect_without_contacting_another_endpoint(status, operation):
    with local_endpoint() as (sink_url, sink_requests):
        with local_endpoint(status=status, location=sink_url + "/v1/models") as (url, requests):
            if operation == "probe":
                result = probe_model_target(target(url), env={}, timeout_seconds=2)
                assert result["reachable"] is True
                assert result["ready"] is False
                assert result["http_status"] == status
            else:
                with pytest.raises(NonRetryableModelError, match=f"HTTP {status}"):
                    adapter(url).respond(messages=[{"role": "user", "content": "private task"}], tools=[])
            assert len(requests) == 1
            assert sink_requests == []


@pytest.mark.parametrize("operation", ["probe", "inference"])
def test_local_requests_ignore_configured_proxy(monkeypatch, operation):
    # Force the platform-independent proxy decision. Windows can otherwise
    # bypass loopback through registry/system settings and conceal this defect.
    monkeypatch.setattr("urllib.request.proxy_bypass", lambda _host: False)
    with local_endpoint() as (proxy_url, proxy_requests):
        monkeypatch.setenv("http_proxy", proxy_url)
        monkeypatch.setenv("https_proxy", proxy_url)
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("HTTPS_PROXY", proxy_url)
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setattr("urllib.request._opener", None)
        with local_endpoint() as (url, requests):
            if operation == "probe":
                result = probe_model_target(target(url), env={}, timeout_seconds=2)
                assert result["ready"] is True
            else:
                result = adapter(url).respond(messages=[{"role": "user", "content": "private task"}], tools=[])
                assert result.final_text == "local response"
            assert len(requests) == 1
            assert proxy_requests == []


@pytest.mark.parametrize("status,category,retry_after", [
    (408, "transient", None), (429, "throttling", "17"),
    (500, "transient", None), (502, "transient", None),
    (503, "transient", None), (504, "transient", None),
])
def test_local_provider_retains_http_retry_policy(status, category, retry_after):
    with local_endpoint(status=status, retry_after=retry_after) as (url, requests):
        with pytest.raises(RetryableModelError) as raised:
            adapter(url).respond(messages=[{"role": "user", "content": "hi"}], tools=[])
        assert raised.value.category == category
        assert raised.value.retry_after_seconds == (17.0 if retry_after else None)
        assert len(requests) == 1


def test_local_provider_retains_nonretryable_client_error():
    with local_endpoint(status=401) as (url, requests):
        with pytest.raises(NonRetryableModelError, match="HTTP 401"):
            adapter(url).respond(messages=[{"role": "user", "content": "hi"}], tools=[])
        assert len(requests) == 1
