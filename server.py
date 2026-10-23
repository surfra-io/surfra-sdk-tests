"""A fake Surfra for SDK tests: the volumes service and the control plane endpoints the SDKs call.

Every SDK runs its conformance tests against this one server, so they agree on behavior. It uses only
the Python standard library, so any CI can run it:

    python3 server.py --port 8780        # prints "listening on http://127.0.0.1:8780" when ready

Point an SDK at it with SURFRA_MASTER and SURFRA_VOLUMES_URL both set to that address. Test helpers:

    POST /_test/reset            clear all state
    GET  /_test/state            files (path -> etag, size), counters, the request log
    POST /_test/files            {"volume", "path", "body"} put a file without counting a request

It mirrors the real volumes service where SDKs depend on it: Content-Length is required on uploads,
uploads above the part size are refused, multipart parts must add up, errors are {"error": "..."} with
the real status codes, and files are always served as application/octet-stream.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

PART_BYTES = int(os.environ.get("SURFRA_VOLUMES_PART_BYTES") or 64 * 1024 * 1024)


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.files: dict[str, dict[str, bytes]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, str] = {}
        self.transactions: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.log: list[dict[str, Any]] = []
        self.downloads = 0
        self.uploads_done = 0
        self.serial = 0
        self.apps: dict[str, dict[str, Any]] = {}
        self.app_volumes: dict[str, list[Any]] = {}
        self.busy_once = True
        self.agent_command = "true"
        self.agent_finished: list[dict[str, Any]] = []

    def next_id(self, prefix: str) -> str:
        self.serial += 1
        return f"{prefix}_{self.serial:08d}"


STATE = State()


def etag(data: bytes) -> str:
    return '"' + hashlib.md5(data).hexdigest() + '"'


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Plumbing --------------------------------------------------------------------------------

    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, status: int, body: Any = None, *, raw: bytes | None = None, headers: dict[str, str] | None = None) -> None:
        payload = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        self.send_response(status)
        if raw is None and body is not None:
            self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _detail(self, status: int, message: str) -> None:
        self._send(status, {"detail": message})

    def _body(self) -> bytes:
        length = self.headers.get("Content-Length")
        return self.rfile.read(int(length)) if length else b""

    def _json(self) -> dict[str, Any]:
        raw = self._body()
        return json.loads(raw) if raw else {}

    def _route(self) -> None:
        url = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        path = url.path
        with STATE.lock:
            STATE.log.append({"method": self.command, "path": path, "query": query, "auth": self.headers.get("Authorization")})
        if path.startswith("/_test/"):
            return self._test(path)
        if path.startswith("/v1/jobs/agent/") or path == "/v1/workers/logs":
            return self._agent(path)
        if path == "/v1/tokens":
            self._body()
            if not (self.headers.get("Authorization") or "").startswith("Bearer sfp_"):
                return self._detail(401, "unknown or expired credential")
            return self._send(200, {"token": "vol.tok.en", "expires_at": int(time.time()) + 900})
        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            return self._error(401, "send a Surfra API key as Authorization: Bearer")
        parts = path.split("/")
        if path.split("/")[2:3] and path.split("/")[2] in ("volumes", "transactions", "jobs", "apps", "workflows"):
            return self._control(path, query)
        if len(parts) >= 4 and parts[1] == "v1":
            return self._volume(unquote(parts[2]), parts[3], "/".join(parts[4:]), query)
        if path == "/v1/usage":
            return self._send(200, {"bytes": 0, "objects": 0, "writes": 0})
        return self._error(404, "unknown route")

    do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _route

    # Test helpers ----------------------------------------------------------------------------

    def _test(self, path: str) -> None:
        if path == "/_test/reset":
            self._body()
            with STATE.lock:
                STATE.reset()
            return self._send(200, {"ok": True})
        if path == "/_test/files":
            body = self._json()
            with STATE.lock:
                STATE.volumes.setdefault(body["volume"], STATE.next_id("vol"))
                STATE.files.setdefault(body["volume"], {})[body["path"]] = body["body"].encode()
            return self._send(200, {"ok": True})
        if path == "/_test/agent":
            body = self._json()
            with STATE.lock:
                STATE.agent_command = body["command"]
            return self._send(200, {"ok": True})
        if path == "/_test/state":
            with STATE.lock:
                files = {volume: {key: {"etag": etag(data), "size": len(data), "body": data.decode(errors="replace")} for key, data in items.items()} for volume, items in STATE.files.items()}
                return self._send(200, {"files": files, "downloads": STATE.downloads, "uploads": STATE.uploads_done, "log": STATE.log, "jobs": STATE.jobs, "agent_finished": STATE.agent_finished})
        return self._error(404, "unknown test route")

    # The job agent's side of the control plane --------------------------------------------

    def _agent(self, path: str) -> None:
        raw = self._body()
        body = json.loads(raw) if path.endswith("/finish") and raw else {}
        if not (self.headers.get("X-Surfra-Replica-Token") or "").startswith("sfp_"):
            return self._detail(401, "unknown replica")
        if path == "/v1/jobs/agent/start":
            with STATE.lock:
                command = STATE.agent_command
            return self._send(200, {"run": True, "command": command, "grace_sec": 5, "heartbeat_sec": 1, "deadline_at": None})
        if path == "/v1/jobs/agent/heartbeat":
            return self._send(200, {"stop": "", "tail": False})
        if path == "/v1/jobs/agent/finish":
            with STATE.lock:
                STATE.agent_finished.append(body or {})
            return self._send(200, {"status": "ok"})
        return self._send(200, {"ok": True})

    # Volumes service -------------------------------------------------------------------------

    def _volume(self, name: str, kind: str, rest: str, query: dict[str, str]) -> None:
        with STATE.lock:
            if name not in STATE.volumes:
                self._body()
                return self._error(404, f"no volume {name}, or this credential cannot reach it")
            files = STATE.files.setdefault(name, {})
        path = unquote(rest)
        method = self.command
        if kind == "list" and method == "GET":
            prefix = query.get("prefix", "")
            recursive = query.get("recursive") in ("1", "true")
            out, folders = [], set()
            with STATE.lock:
                for key, data in sorted(files.items()):
                    if not key.startswith(prefix):
                        continue
                    tail = key[len(prefix):]
                    if not recursive and "/" in tail:
                        folders.add(prefix + tail.split("/")[0] + "/")
                        continue
                    out.append({"path": key, "size": len(data), "etag": etag(data), "modified": "2026-10-22T00:00:00.000Z"})
            return self._send(200, {"files": out, "folders": sorted(folders), "cursor": None})
        if kind == "files":
            if method == "PUT":
                if self.headers.get("Content-Length") is None:
                    return self._error(411, "send Content-Length; streamed bodies of unknown size are not accepted")
                if int(self.headers["Content-Length"]) > PART_BYTES:
                    self._body()
                    return self._error(413, f"this request is limited to {PART_BYTES} bytes; larger files upload in parts")
                data = self._body()
                with STATE.lock:
                    current = files.get(path)
                    wanted = self.headers.get("If-Match")
                    if wanted and (current is None or etag(current) != wanted):
                        return self._error(412, "the file changed since that ETag")
                    files[path] = data
                    STATE.uploads_done += 1
                return self._send(200, {"path": path, "size": len(data), "etag": etag(data)})
            self._body()
            with STATE.lock:
                data = files.get(path)
            if data is None:
                return self._error(404, "no such file")
            if method == "DELETE":
                with STATE.lock:
                    del files[path]
                return self._send(200, {"deleted": path})
            headers = {"ETag": etag(data), "Content-Type": "application/octet-stream", "Content-Disposition": "attachment"}
            if method == "HEAD":
                self.send_response(200)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return None
            with STATE.lock:
                STATE.downloads += 1
            return self._send(200, raw=data, headers=headers)
        if kind == "delete" and method == "POST":
            body = self._json()
            with STATE.lock:
                if "prefix" in body:
                    gone = [key for key in files if key.startswith(body["prefix"])]
                else:
                    gone = [key for key in body.get("paths") or [] if key in files]
                for key in gone:
                    del files[key]
            return self._send(200, {"deleted": len(gone), "more": False})
        if kind == "uploads":
            return self._multipart(name, files, rest, query)
        self._body()
        return self._error(405, "method not allowed here")

    def _multipart(self, name: str, files: dict[str, bytes], rest: str, query: dict[str, str]) -> None:
        method = self.command
        if not rest and method == "POST":
            body = self._json()
            upload = STATE.next_id("up")
            with STATE.lock:
                STATE.uploads[upload] = {"volume": name, "path": body["path"], "size": int(body["size"]), "parts": {}}
            return self._send(200, {"upload_id": upload, "part_size": PART_BYTES, "previous_size": None})
        bits = rest.split("/")
        upload = unquote(bits[0])
        with STATE.lock:
            found = STATE.uploads.get(upload)
        if not found:
            self._body()
            return self._error(404, "unknown or expired upload")
        if len(bits) == 2 and bits[1] == "complete" and method == "POST":
            body = self._json()
            chunks = found["parts"]
            data = b"".join(chunks[int(item["part"])] for item in sorted(body["parts"], key=lambda item: int(item["part"])))
            with STATE.lock:
                files[body["path"]] = data
                STATE.uploads_done += 1
                del STATE.uploads[upload]
            return self._send(200, {"path": body["path"], "size": len(data), "etag": etag(data)})
        if len(bits) == 2 and method == "PUT":
            if self.headers.get("Content-Length") is None:
                return self._error(411, "send Content-Length")
            data = self._body()
            with STATE.lock:
                total = sum(len(chunk) for chunk in found["parts"].values()) + len(data)
                if total > found["size"]:
                    return self._error(413, "the parts add up to more than the size given when the upload started")
                found["parts"][int(bits[1])] = data
            return self._send(200, {"part": int(bits[1]), "etag": f"part-{bits[1]}"})
        if len(bits) == 1 and method == "DELETE":
            self._body()
            with STATE.lock:
                del STATE.uploads[upload]
            return self._send(200, {"aborted": upload})
        self._body()
        return self._error(405, "method not allowed here")

    # Control plane ---------------------------------------------------------------------------

    def _control(self, path: str, query: dict[str, str]) -> None:
        method = self.command
        parts = [unquote(part) for part in path.split("/")[2:]]
        body: dict[str, Any] = {}
        if method in ("POST", "PUT"):
            body = self._json()
        else:
            self._body()
        if parts[0] == "volumes":
            if method == "POST" and len(parts) == 1:
                with STATE.lock:
                    volume_id = STATE.volumes.setdefault(body["name"], STATE.next_id("vol"))
                    STATE.files.setdefault(body["name"], {})
                return self._send(200, {"id": volume_id, "name": body["name"], "bytes": 0, "objects": 0, "used_by": []})
            if method == "GET" and len(parts) == 1:
                with STATE.lock:
                    rows = [{"id": vid, "name": name, "bytes": sum(len(d) for d in STATE.files.get(name, {}).values()), "objects": len(STATE.files.get(name, {})), "used_by": []} for name, vid in sorted(STATE.volumes.items())]
                return self._send(200, {"volumes": rows})
            name = parts[1]
            with STATE.lock:
                exists = name in STATE.volumes
            if not exists:
                return self._detail(404, "unknown volume")
            if method == "DELETE":
                with STATE.lock:
                    del STATE.volumes[name]
                    STATE.files.pop(name, None)
                return self._send(200, {"deleted": name})
            return self._send(200, {"id": STATE.volumes[name], "name": name, "bytes": 0, "objects": 0, "used_by": []})
        if parts[0] == "transactions":
            if method == "POST" and any(stage.get("app_name") == "busy" for stage in body.get("stages") or []):
                with STATE.lock:
                    first, STATE.busy_once = STATE.busy_once, False
                if first:
                    return self._send(429, {"detail": "over the request rate"}, headers={"Retry-After": "1"})
            if method == "POST":
                transaction = STATE.next_id("tx")
                stages = [{"name": stage["name"], "app_name": stage["app_name"], "status": "succeeded", "output": {"app": stage["app_name"], "echo": stage.get("input")}} for stage in body.get("stages") or []]
                if any(stage["app_name"] == "fails" for stage in stages):
                    for stage in stages:
                        stage["status"], stage["output"] = "failed", {"error": "the handler raised ValueError"}
                with STATE.lock:
                    STATE.transactions[transaction] = {"id": transaction, "status": "queued", "stages": stages, "polls": 0}
                return self._send(200, {"id": transaction, "status": "queued", "stages": [{**stage, "status": "queued", "output": None} for stage in stages]})
            with STATE.lock:
                found = STATE.transactions.get(parts[1])
                if found:
                    found["polls"] += 1
            if not found:
                return self._detail(404, "unknown transaction")
            # The first poll sees it running; later polls see the result.
            if found["polls"] < 2:
                return self._send(200, {"id": found["id"], "status": "running", "stages": [{**stage, "status": "running", "output": None} for stage in found["stages"]]})
            failed = any(stage["status"] == "failed" for stage in found["stages"])
            return self._send(200, {"id": found["id"], "status": "failed" if failed else "succeeded", "stages": found["stages"]})
        if parts[0] == "jobs":
            if method == "POST" and len(parts) == 1:
                job = STATE.next_id("job")
                view = {"id": job, "name": body.get("name") or "job", "status": "queued", "exit_code": None, "volumes": body.get("volumes") or [], "image": body.get("image"), "command": body.get("entrypoint"), "polls": 0}
                with STATE.lock:
                    STATE.jobs[job] = view
                return self._send(200, {key: value for key, value in view.items() if key != "polls"})
            with STATE.lock:
                found = STATE.jobs.get(parts[1])
            if not found:
                return self._detail(404, "unknown job")
            if len(parts) == 3 and parts[2] == "logs":
                lines = [{"t": f"2026-10-22T00:00:0{n}.000Z", "s": "out", "m": f"line {n}"} for n in range(3)]
                after = query.get("after")
                return self._send(200, {"lines": [line for line in lines if not after or line["t"] > after]})
            if len(parts) == 3 and parts[2] == "stop":
                found["status"] = "stopped"
            else:
                found["polls"] += 1
                if found["status"] == "queued":
                    found["status"] = "running"
                elif found["status"] == "running" and found["polls"] >= 3:
                    found["status"], found["exit_code"] = "succeeded", 0
            return self._send(200, {key: value for key, value in found.items() if key != "polls"})
        if parts[0] == "apps" and len(parts) == 3 and parts[2] == "volumes":
            with STATE.lock:
                if method == "PUT":
                    STATE.app_volumes[parts[1]] = [item if isinstance(item, dict) else {"volume": item.split(":")[0], "mode": (item.split(":") + ["r"])[1]} for item in body.get("volumes", [])]
                return self._send(200, {"volumes": STATE.app_volumes.get(parts[1], [])})
        if parts[0] == "apps" and len(parts) == 2:
            with STATE.lock:
                if method == "PUT":
                    found = STATE.apps.setdefault(parts[1], {"id": STATE.next_id("sfa"), "name": parts[1]})
                    found.update({key: body.get(key) for key in ("image", "entrypoint", "env", "registry", "autoscale", "runtime") if key in body})
                found = STATE.apps.get(parts[1])
            if not found:
                return self._detail(404, "unknown app")
            return self._send(200, found)
        if parts[0] == "workflows" and len(parts) == 2:
            stages = [{"name": "a", "app": "embed", "depends": []}, {"name": "b", "app": "rerank", "depends": ["a"]}]
            return self._send(200, {"name": parts[1], "stages": stages})
        return self._detail(404, "unknown route")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8780)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
