"""Every SDK inside a real job agent, with nothing configured.

Runs the Go `surfra job agent` against the fake. The job's command uses each SDK in turn to write and read a
file in a volume. It passes when every file lands, every volume request carried the volumes-only token
(through the agent's local proxy or the token file), and no request carried the replica token.

    python3 container_check.py --agent ../surfra-worker/dist/surfra-linux-amd64 \\
        --python ../surfra-python-sdk/src --node-sdk ../surfra-typescript/dist/node.js \\
        --rust ../surfra-rust/target/debug/examples/container --go ../surfra-go/container-example
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent


def call(url: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url + path, data=data, method="POST" if data is not None else "GET", headers={"Authorization": "Bearer sfk_setup"})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--python")
    parser.add_argument("--node-sdk")
    parser.add_argument("--rust")
    parser.add_argument("--go")
    args = parser.parse_args()

    server = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", "0"], stdout=subprocess.PIPE, text=True)
    url = server.stdout.readline().strip().removeprefix("listening on ")
    try:
        call(url, "/v1/volumes", {"name": "weights"})
        steps, expected = [], []
        if args.python:
            steps.append(f"PYTHONPATH={shlex.quote(args.python)} {shlex.quote(sys.executable)} -c " + shlex.quote("import surfra; v = surfra.Volume('weights'); v.write('from/python.txt', 'python'); print('python ok', v.read('from/python.txt'))"))
            expected.append("from/python.txt")
        if args.node_sdk:
            script = f"import({json.dumps(str(Path(args.node_sdk).resolve()))}).then(async (m) => {{ const v = new m.Volume('weights'); await v.write('from/ts.txt', 'typescript'); console.log('typescript ok', await v.readText('from/ts.txt')); }})"
            steps.append("node -e " + shlex.quote(script))
            expected.append("from/ts.txt")
        if args.rust:
            steps.append(shlex.quote(str(Path(args.rust).resolve())))
            expected.append("from/rust.txt")
        if args.go:
            steps.append(shlex.quote(str(Path(args.go).resolve())))
            expected.append("from/go.txt")
        call(url, "/_test/agent", {"command": " && ".join(steps)})

        home = tempfile.mkdtemp()
        env = {key: value for key, value in os.environ.items() if not key.startswith("SURFRA_")}
        env.update(HOME=home, SURFRA_MASTER=url, SURFRA_REPLICA_TOKEN="sfp_check", SURFRA_VOLUMES_URL=url, SURFRA_LOG_DIR=tempfile.mkdtemp())
        began = time.monotonic()
        agent = subprocess.run([args.agent, "job", "agent"], env=env, capture_output=True, text=True, timeout=300)
        print(agent.stdout.strip())
        if agent.stderr.strip():
            print(agent.stderr.strip(), file=sys.stderr)

        state = call(url, "/_test/state")
        files = state["files"].get("weights", {})
        volume_requests = [item for item in state["log"] if item["path"].startswith("/v1/weights/")]
        problems = []
        problems += [f"missing {path}" for path in expected if path not in files]
        problems += [f"{item['method']} {item['path']} sent {item['auth']}" for item in volume_requests if item["auth"] != "Bearer vol.tok.en"]
        problems += [f"the replica token reached {item['path']}" for item in state["log"] if "sfp_" in (item["auth"] or "") and item["path"] != "/v1/tokens"]
        finished = state["agent_finished"]
        if agent.returncode != 0 or not finished or finished[-1].get("exit_code") != 0:
            problems.append(f"the job did not succeed: agent exit {agent.returncode}, reported {finished}")
        if problems:
            print("FAILED:\n  " + "\n  ".join(problems))
            return 1
        print(f"passed in {time.monotonic() - began:.1f}s: {', '.join(expected)} written through the job's volumes token by {len(volume_requests)} requests")
        return 0
    finally:
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())
