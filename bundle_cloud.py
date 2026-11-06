"""Build the small, source-only variable bundle used by the Railway deployment."""

import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path

RUNTIME_FILES = ("__init__.py", "__main__.py", "core.py", "settings.py", "web.py", "monitor.py", "runner.py")


def build_bundle(project):
    files = {"iclr_alert/" + name: base64.b64encode((project / "iclr_alert" / name).read_bytes()).decode("ascii") for name in RUNTIME_FILES}
    compressed = gzip.compress(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8"), mtime=0)
    variables = {
        "ICLR_SOURCE_B64": base64.b64encode(compressed).decode("ascii"),
        "ICLR_SOURCE_SHA256": hashlib.sha256(compressed).hexdigest(),
        "ICLR_BOOTSTRAP_B64": base64.b64encode((project / "bootstrap_cloud.py").read_bytes()).decode("ascii"),
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }
    if any(len(value.encode("utf-8")) > 32768 for value in variables.values()):
        raise ValueError("Runtime bundle exceeds the Railway per-variable size limit.")
    return variables


def main():
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=project / ".local" / "railway-source-variables.json")
    args = parser.parse_args()
    variables = build_bundle(project)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(variables), encoding="utf-8")
    print("Source bundle written:", args.output)
    print("SHA256:", variables["ICLR_SOURCE_SHA256"])


if __name__ == "__main__":
    main()
