"""Emit a GitHub Actions matrix slice of the reviewed Windows v1 -> v2 batch."""
import json
import os
import sys
from pathlib import Path

ITEMS = Path("runtime-windows/v1-v2-batch.json")
items = json.loads(ITEMS.read_text())
offset = int(os.environ["OFFSET"])
limit = int(os.environ["LIMIT"])
batch = items[offset:offset + limit]
payload = json.dumps({"include": batch}, separators=(",", ":"))
print(f"batch offset={offset} limit={limit} count={len(batch)} matrix_bytes={len(payload)}", file=sys.stderr)
with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
    output.write("matrix=" + payload + "\n")
    output.write(f"count={len(batch)}\n")
