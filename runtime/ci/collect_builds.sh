#!/usr/bin/env bash
# Record exact-run macOS v2 results with this repository's GITHUB_TOKEN only.
set -euo pipefail
: "${GITHUB_RUN_ID:?}"
: "${GITHUB_RUN_ATTEMPT:?}"
: "${GITHUB_REPOSITORY:?}"
: "${RUNNER_TEMP:?}"
: "${PRODUCER_COMMIT:?}"
: "${BUILD_RECORDS_JSON:?}"
script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
work="${RUNNER_TEMP}/macos-v2-collection"
mkdir -p "$work"
export COLLECTION_WORK="$work"
python3 - <<'PY'
import json, os
from pathlib import Path
work = Path(os.environ['COLLECTION_WORK'])
records = json.loads(os.environ['BUILD_RECORDS_JSON'])
producer = {'commit': os.environ['PRODUCER_COMMIT'], 'run_id': os.environ['GITHUB_RUN_ID'],
            'run_attempt': int(os.environ['GITHUB_RUN_ATTEMPT'])}
if os.environ.get('INCLUDE_CURRENT_RUN_MODULES') == 'true':
    import importlib.util
    helper = Path('runtime-v2/ci/module_pipeline.py')
    spec = importlib.util.spec_from_file_location('collection_modules', helper)
    pipeline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipeline)
    read, _ = pipeline._state.github_state_store(os.environ['GITHUB_REPOSITORY'], 'runtime-v2/config/build-state.json')
    _, latest = read()
    records = {'include': pipeline.current_run_records(records['include'], latest, producer)}
(work / 'records.json').write_text(json.dumps(records))
(work / 'producer.json').write_text(json.dumps(producer))
PY
python3 "${script_dir}/build_state.py" collect \
  --state "${repo_root}/runtime-v2/config/build-state.json" \
  --records "$work/records.json" \
  --producer "$work/producer.json" --artifact-dir "$work/archives" \
  --repository "$GITHUB_REPOSITORY" --persist --report "$work/report.json"
python3 - <<'PY'
import json, os
from pathlib import Path
report = json.loads((Path(os.environ['COLLECTION_WORK']) / 'report.json').read_text())
print(json.dumps(report, indent=2))
if os.environ.get('GITHUB_OUTPUT'):
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write(f"ready_count={report['ready_count']}\n")
PY
