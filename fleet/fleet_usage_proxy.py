"""Fleet usage proxy — transparent logging shim in front of local Ollama.

Sits on the port every fleet client already hits (default 11434) and forwards
to the real Ollama upstream (default 127.0.0.1:11436). For /api/generate and
/api/chat it tees the response stream to capture exact token counts
(prompt_eval_count / eval_count) into the fleet usage ledger, so EVERY local
Gemma/Ollama call — router-routed or hardcoded — is tracked going forward.

Inference correctness comes first: bytes are forwarded faithfully (streaming
preserved); logging is a best-effort side effect that can never break a call.

Env:
  FLEET_PROXY_HOST       bind host        (default 127.0.0.1)
  FLEET_PROXY_PORT       listen port      (default 11434)
  FLEET_OLLAMA_UPSTREAM  real ollama url  (default http://127.0.0.1:11436)
"""
import os
import sys
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_api", "services"))
from fleet_usage_log import log_fleet_usage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [fleet-proxy] %(message)s")
logger = logging.getLogger("fleet_usage_proxy")

UPSTREAM = os.environ.get("FLEET_OLLAMA_UPSTREAM", "http://127.0.0.1:11436").rstrip("/")
LOG_PATHS = ("/api/generate", "/api/chat")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-length"}


def _record_from_final(obj, path):
    """Pull token counts out of a final ollama response object and log them."""
    try:
        if not isinstance(obj, dict):
            return
        if obj.get("prompt_eval_count") is None and obj.get("eval_count") is None:
            return
        log_fleet_usage(
            provider="Ollama (local)",
            model=obj.get("model") or "unknown",
            input_tokens=obj.get("prompt_eval_count") or 0,
            output_tokens=obj.get("eval_count") or 0,
            lane="local",
            source=f"ollama-proxy{path}",
        )
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fleet-usage-proxy/1.0"

    def log_message(self, *args):  # silence default access logging
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else None

    def _fwd_headers(self):
        return {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}

    def _proxy(self, method):
        body = self._body()
        url = UPSTREAM + self.path
        try:
            up = requests.request(method, url, headers=self._fwd_headers(), data=body,
                                  stream=True, timeout=600)
        except Exception as e:
            self.send_error(502, f"upstream unreachable: {e}")
            return

        self.send_response(up.status_code)
        for k, v in up.headers.items():
            if k.lower() not in HOP_BY_HOP:
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

        log_this = method == "POST" and self.path in LOG_PATHS
        last_json = None
        try:
            for chunk in up.iter_lines(decode_unicode=False):
                if chunk is None:
                    continue
                # Faithfully forward the line (ollama streams NDJSON).
                self.wfile.write(chunk + b"\n")
                self.wfile.flush()
                if log_this and chunk.strip():
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict) and (obj.get("done") or obj.get("eval_count") is not None):
                            last_json = obj
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"stream relay error on {self.path}: {e}")
        finally:
            up.close()

        if log_this and last_json is not None:
            _record_from_final(last_json, self.path)

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_PUT(self):
        self._proxy("PUT")

    def do_HEAD(self):
        self._proxy("HEAD")


def main():
    host = os.environ.get("FLEET_PROXY_HOST", "127.0.0.1")
    port = int(os.environ.get("FLEET_PROXY_PORT", "11434"))
    logger.info(f"listening on {host}:{port} -> upstream {UPSTREAM}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
