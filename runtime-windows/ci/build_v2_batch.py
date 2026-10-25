"""Build the Windows v1-aligned v2 batch list using the v2 producer's major rules."""
import argparse
import importlib.util
import json
from pathlib import Path

import requests
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
EXCLUDED = {"php", "postgresql", "simple-acme"}
API = "https://www.servbay.com/api/v1/package"
PARAMS = {
    "uid": "11111111-1111-1111-1111-111111111111",
    "app_version": "1.33.0",
    "os": "Windows",
    "cpu_arch": "x64",
    "runtime_version": "v1",
}


def load_metadata():
    spec = importlib.util.spec_from_file_location("servbay_package_meta", ROOT / "runtime/ci/package_meta.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.package_metadata


def load_catalog(path):
    if path:
        payload = json.loads(Path(path).read_text())
    else:
        payload = requests.get(API, params=PARAMS, timeout=(10, 30)).json()
    return payload["data"]["packages"]


def build(packages, package_metadata):
    selected = {}
    skipped = []
    for package in packages:
        name = package["name"]
        if name in EXCLUDED:
            continue
        for row in package["versions"]:
            version = row["latestVersion"]
            major = package_metadata(name, version)["major"]
            key = (name, major)
            current = selected.get(key)
            if current is None or Version(version) > Version(current["version"]):
                selected[key] = {
                    "pkg": name,
                    "version": version,
                    "source_url": row["downloadUrl"],
                    "source_sha256": row["hash"],
                    "minimum_os": "10.0",
                }
            else:
                skipped.append({"pkg": name, "version": version, "v1_major": row["majorVersion"], "reason": f"same v2 major {major} as {current['version']}"})
    return [selected[key] for key in sorted(selected)], skipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", help="saved v1 API response; fetch live when omitted")
    parser.add_argument("--output", default="runtime-windows/v1-v2-batch.json")
    args = parser.parse_args()
    items, skipped = build(load_catalog(args.catalog), load_metadata())
    Path(args.output).write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    print(f"items={len(items)} packages={len({i['pkg'] for i in items})}")
    for row in skipped:
        print(f"skipped {row['pkg']} {row['version']} (v1 major {row['v1_major']}): {row['reason']}")


if __name__ == "__main__":
    main()
