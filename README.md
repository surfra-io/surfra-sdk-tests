# surfra-sdk-tests

The cases every Surfra SDK must pass, and a fake Surfra to run them against. See [CASES.md](CASES.md).

```bash
python3 server.py --port 8780          # the fake: volumes service and control plane, standard library only
python3 check.py ../surfra-rust/tests  # fails if an SDK's tests miss a case id
```
