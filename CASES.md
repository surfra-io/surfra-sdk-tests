# Surfra SDK conformance

Every Surfra SDK (Python, Rust, TypeScript, Go) implements the cases below as tests against `server.py`,
and names each test with its case id, such as `v01_small_upload_round_trips`. `check.py <sdk dir>` fails
if an id is missing, so SDKs cannot drift apart silently.

Run the server, then point the SDK at it:

```bash
python3 server.py --port 8780 &
export SURFRA_MASTER=http://127.0.0.1:8780 SURFRA_VOLUMES_URL=http://127.0.0.1:8780 SURFRA_API_KEY=sfk_test
export SURFRA_VOLUMES_PART_BYTES=1024 SURFRA_VOLUME_CACHE=$(mktemp -d)
```

Each test starts with `POST /_test/reset`, then creates its volumes with `POST /v1/volumes` or seeds files
with `POST /_test/files`, and checks the result with `GET /_test/state`.

## Shared behavior

**Settings.** `SURFRA_API_KEY`, `SURFRA_MASTER` (default `https://api.surfra.io`), `SURFRA_VOLUMES_URL`
(default `https://volumes.surfra.io`), `SURFRA_VOLUME_CACHE` (default `<temp dir>/surfra`), and
`SURFRA_VOLUMES_PART_BYTES` (default 67108864, the largest single upload; a test knob).

**Credentials, in order.** An explicit key; the file named by `SURFRA_VOLUMES_TOKEN_FILE`, read again on
every request; `SURFRA_API_KEY`; `SURFRA_REPLICA_TOKEN`; the session saved by `surfra login` in
`~/.surfra/config.json` under `"token"`. None of these: an error that says to set `SURFRA_API_KEY`.

**Errors.** A failed request raises the SDK's error type carrying the HTTP status and the service's reason,
read from `{"error": ...}` (volumes) or `{"detail": ...}` (control plane).

**Uploads.** A file up to the part size goes up in one `PUT` with `Content-Length`. Larger files start a
multipart upload, send parts of the server's `part_size`, and complete it; on failure the SDK aborts it.
Writing to a volume that does not exist yet (`404` whose reason starts with "no volume") creates it with
`POST /v1/volumes` and retries once.

**The sync cache.** All SDKs share one layout, so code in different languages in one container agrees:

- Files: `<cache>/vol-<name>/<path>`.
- State: `<cache>/.state/vol-<name>.json`, a JSON object mapping each synced path to
  `{"etag": "...", "size": 123, "mtime_ns": 1729588000000000000}`. `mtime_ns` is the local file's
  modification time in integer nanoseconds after the sync wrote it.
- `local(path)` lists the prefix recursively (or `HEAD`s a single file), downloads each entry whose ETag,
  size or local `mtime_ns` differ from the state (or whose file is missing), writing to a temporary file
  and renaming it into place, removes local files that an earlier sync wrote and the volume no longer has,
  and saves the state. A second `local` of the same path on the same volume object makes no request unless
  `refresh` is set.
- `commit()` walks every folder synced with mode `w` or `rw`, uploads files that are new or whose size or
  `mtime_ns` changed, deletes from the volume files the state lists that are gone locally (mode `rw`
  only), and saves the state. It returns how many it uploaded and deleted.

## Cases

| Id | Behavior |
| --- | --- |
| v01 | A small file uploads in one `PUT` and reads back unchanged with `open` (read) |
| v02 | A file larger than the part size uploads in parts and reads back unchanged |
| v03 | `put_directory` uploads every file under the remote prefix, keeping relative paths |
| v04 | `ls` without recursion returns files and folders (folders end in `/`); recursive returns every file |
| v05 | `local` on a folder downloads its files to the cache; calling it again makes no request |
| v06 | A second volume object on the same cache downloads only changed files and deletes removed ones |
| v07 | `local(mode=rw)` then `commit` uploads new and changed files and deletes removed ones; a second `commit` does nothing |
| v08 | `open` for writing uploads on close; a write that fails before close uploads nothing |
| v09 | `rm` deletes a file; `rm` recursive deletes everything under a folder |
| v10 | A missing file raises the SDK error with status 404 and the reason "no such file" |
| v11 | A token in `SURFRA_VOLUMES_TOKEN_FILE` is sent instead of `SURFRA_API_KEY` |
| v12 | With no credential anywhere, using a volume raises an error that mentions `SURFRA_API_KEY` |
| v13 | Writing to a volume that does not exist yet creates it and the write succeeds |
| v14 | A state file written by another SDK is honored: files it lists with matching ETag, size and `mtime_ns` are not downloaded again |
| a01 | `run(app, input)` returns the output: the fake answers `{"app": <app>, "echo": <input>}` |
| a02 | `enqueue` returns a call; its `result` returns the same output; a failed call raises with the stage's error |
| j01 | Submitting a job with volumes sends them; waiting returns status `succeeded` and exit code 0 |
