"""Emit a GitHub Actions matrix slice of the reviewed Windows v1 -> v2 batch."""
import json
import os
from pathlib import Path

ITEMS = Path("runtime-windows/v1-v2-batch.json")
items = json.loads(ITEMS.read_text())
offset = int(os.environ["OFFSET"])
limit = int(os.environ["LIMIT"])
batch = items[offset:offset + limit]
with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
    output.write("matrix=" + json.dumps({"include": batch}, separators=(",", ":")) + "\n")
    output.write(f"count={len(batch)}\n")
