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

**Control-plane credentials.** Calls to the control plane (apps, workflows, jobs, volume names) use an
explicit key, then `SURFRA_API_KEY`, then the saved login. They never send the volumes token from
`SURFRA_VOLUMES_TOKEN_FILE`, which only the volumes service accepts. Inside a job, `SURFRA_API_KEY` is the
job's own key when the job may call apps.

**Rate limits.** Submitting a call or a workflow run that gets `429` waits for `Retry-After` (at least one
second, at most 60) and tries again, up to five times.

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

## The surface

Every SDK has these, named the way its language names things (`put_file`, `putFile`, `PutFile`):

| Object | Has |
| --- | --- |
| Volume | `path(p).local(mode, refresh)`, `path(p).download(dest)`, `read`, `write`, `put_file`, `put_directory`, `ls(prefix, recursive)`, `rm` (and recursive), `commit`, `create`, `info`, `delete`, list all volumes, `grant(mode)` |
| App | `deploy(image, entrypoint, env, autoscale, runtime)`, `run(input)`, `enqueue(input)`, `info`, `volumes`, `set_volumes` |
| Workflow | `deploy(stages)`, `run(input)`, `enqueue(input)` |
| Call | from `submit`'s view; `get`, `result` |
| Jobs | `submit_job(spec)`, `job(id)`; a job has `get`, `stop`, `wait`, `logs(after)` |

Language-specific: the Python handler runtime (`@surfra.handler`, `warmup`, `batch_size`, `progress`,
stages) and the `surfra` CLI. Apps in other languages are HTTP servers, which the Surfra supervisor runs.

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
| v15 | `write` then `read` round-trips bytes without a local file |
| c01 | With both a volumes token file and an API key, control-plane calls send the API key and volume calls send the token |
| a03 | `App(name).deploy(...)` sends `PUT /v1/apps/<name>` with the image, entrypoint and autoscale, and returns the saved app |
| a04 | `App(name).run(input)` returns the output; `set_volumes(["weights:r"])` then `volumes()` returns `[{"volume": "weights", "mode": "r"}]` |
| a05 | A call submitted while the server answers `429` once waits and succeeds |
| a06 | `submit(stages)` with two stages, wrapped in a call, returns `{stage: output}` from `result` |
| w01 | `Workflow(name).run(input)` reads the workflow, runs it, and returns `{stage: output}` for its two stages |
| w02 | `Workflow(name).deploy(stages)` sends `PUT /v1/workflows/<name>` with the stages; a run afterwards uses them |
| j01 | Submitting a job with volumes sends them; waiting returns status `succeeded` and exit code 0 |
| j02 | A job's `logs` returns its lines oldest first, as `{"t", "s", "m"}` |
