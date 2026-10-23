#!/usr/bin/env bash
# Reserve exact v2 units before a paid job can start. v1 keeps its old path.
set -euo pipefail
: "${RUNTIME_VERSION:?}"
script_dir="$(cd "$(dirname "$0")" && pwd)"
if [ "$RUNTIME_VERSION" = v2 ]; then
  : "${GITHUB_SHA:?}"
  : "${GITHUB_RUN_ID:?}"
  : "${GITHUB_RUN_ATTEMPT:?}"
  : "${GITHUB_REPOSITORY:?}"
  : "${RUNNER_TEMP:?}"
  export CHECKOUT_SHA="$(git rev-parse HEAD)"
  python3 - <<'PYCODE'
import json, os, sys
from pathlib import Path
sys.path.insert(0, 'runtime/ci')
import build_control
build_control.run_decision('v2', False, False, True, os.environ['GITHUB_SHA'], os.environ['CHECKOUT_SHA'])
Path('producer.json').write_text(json.dumps({'commit': os.environ['GITHUB_SHA'], 'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': int(os.environ['GITHUB_RUN_ATTEMPT'])}))
PYCODE
  python3 "$script_dir/build_state.py" reserve --state runtime-v2/config/build-state.json \
    --records matrix.json --matrix matrix.json --producer producer.json \
    --repository "$GITHUB_REPOSITORY" --artifact-dir "$RUNNER_TEMP/v2-reservation-recovery" --runners
fi
python3 - <<'PYCODE' >> "$GITHUB_OUTPUT"
import json, subprocess
from pathlib import Path
matrix = json.loads(Path('matrix.json').read_text())
print('matrix=' + json.dumps(matrix, separators=(',', ':')))
print('count=' + str(len(matrix['include'])))
print('commit=' + subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip())
PYCODE
