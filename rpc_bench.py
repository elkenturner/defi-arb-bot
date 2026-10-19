"""Measure how many upstream RPC requests one pipeline cycle costs, to size a private RPC plan.

Runs one normal pipeline cycle with the forks pointed at a local counting proxy instead of the upstream. The proxy
forwards every request to BASE_RPC_URL (or the public endpoint) and tallies JSON-RPC methods, batch sizes, HTTP errors
and rate limits. Providers bill per method ("compute units"), so the report is per method: multiply by your plan's
price table. Fork only, read-only upstream: nothing is ever signed or sent to a real network.

    python rpc_bench.py --workers 2            # whole seed list
    python rpc_bench.py --workers 4 --tokens 0x..,0x..
"""
import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pipeline
import scanner

ROOT = Path(__file__).resolve().parent


class CountingProxy:
    """Local HTTP JSON-RPC proxy that forwards to `upstream` and counts every call. The upstream URL stays inside."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.methods, self.http = Counter(), Counter()
        self.requests = self.batches = 0
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    msg = json.loads(body)
                except ValueError:
                    msg = {}
                calls = msg if isinstance(msg, list) else [msg]
                with proxy.lock:
                    proxy.requests += 1
                    proxy.batches += isinstance(msg, list)
                    proxy.methods.update(c.get("method", "?") for c in calls)
                req = urllib.request.Request(proxy.upstream, body, {"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=120) as r:
                        status, data = r.status, r.read()
                except urllib.error.HTTPError as e:
                    status, data = e.code, e.read()
                except Exception:
                    status, data = 502, b'{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"upstream error"}}'
                with proxy.lock:
                    proxy.http[status] += 1
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        ThreadingHTTPServer.request_queue_size = 256
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def main():
    ap = argparse.ArgumentParser(description="Count upstream RPC requests for one pipeline cycle")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--tokens", help="comma-separated addresses instead of the seed list")
    ap.add_argument("--size-search", action="store_true")
    ap.add_argument("--out", help="write the report as JSON here")
    args = ap.parse_args()

    upstream = pipeline.FORK_URL
    tokens = ([(a.strip()[:8], a.strip()) for a in args.tokens.split(",")] if args.tokens
              else [(s, a) for s, a, _ in pipeline.SEED_TOKENS])
    # A throwaway copy of the blacklist: runs being compared see the same token set, and the real file is untouched.
    bl_copy = ROOT / "logs" / f"rpc_bench_blacklist_{int(time.time())}.json"
    bl_copy.write_text(pipeline.BLACKLIST_PATH.read_text() if pipeline.BLACKLIST_PATH.exists() else "{}")
    proxy = CountingProxy(upstream)
    print(f"rpc_bench: {len(tokens)} tokens, {args.workers} worker(s), upstream {scanner.redact_url(upstream)}")
    t0 = time.time()
    try:
        results = pipeline.run_cycle(tokens, scanner.ScanSettings(size_search=args.size_search), args.workers,
                                     pipeline.Blacklist(bl_copy), fork_url=proxy.url, base_port=8720)
    finally:
        proxy.close()
    seconds = time.time() - t0
    status = Counter(r["status"] for r in results)
    report = {"workers": args.workers, "tokens": len(tokens), "seconds": round(seconds),
              "http_requests": proxy.requests, "batched_http_requests": proxy.batches,
              "rpc_calls": sum(proxy.methods.values()), "http_status": dict(proxy.http),
              "methods": dict(proxy.methods.most_common()), "statuses": dict(status),
              "upstream": scanner.redact_url(upstream)}
    print(f"\n{report['rpc_calls']:,} RPC calls in {report['http_requests']:,} HTTP requests over {seconds / 60:.1f} min "
          f"({report['rpc_calls'] / max(seconds, 1):.1f}/s); HTTP status {dict(proxy.http)}")
    for m, n in proxy.methods.most_common():
        print(f"  {m:<32}{n:>10,}")
    print(f"token statuses: {dict(status)}")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
