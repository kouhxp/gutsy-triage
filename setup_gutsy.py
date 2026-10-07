#!/usr/bin/env python3
"""Keep one model entry from gutsy's models.example.json, download its files, write models.json.

Usage: setup_gutsy.py EXAMPLE_JSON MODELS_DIR OUT_JSON MODEL_STEM

The example file is read rather than hard-coded so this keeps working if the runtime's
config layout changes: any object with a "gguf" key counts as a model entry.
"""

import json
import os
import sys

from huggingface_hub import hf_hub_download

HF_REPO = os.environ.get("GUTSY_HF_REPO", "kouhxp/gutsy")


def find_entries(obj):
    """Return (container, key) for every dict with a "gguf" key, at any depth."""
    found = []
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else []
    for key, value in items:
        if isinstance(value, dict) and "gguf" in value:
            found.append((obj, key))
        else:
            found.extend(find_entries(value))
    return found


def stem(path):
    return os.path.basename(path).removesuffix(".gguf")


def main():
    example_path, models_dir, out_path, want = sys.argv[1:5]
    with open(example_path) as f:
        config = json.load(f)

    entries = find_entries(config)
    if not entries:
        sys.exit(f"::error::no model entries found in {example_path}")

    keep = [(c, k) for c, k in entries if stem(c[k]["gguf"]) == want]
    if not keep:
        available = ", ".join(stem(c[k]["gguf"]) for c, k in entries)
        sys.exit(f"::error::model '{want}' not in models.example.json (available: {available})")
    container, key = keep[0]
    entry = container[key]

    # Drop every other entry; the runtime expects each listed model to exist on disk.
    for c, k in reversed(entries):
        if not (c is container and k == key):
            del c[k]

    if "default" in config:
        config["default"] = key if isinstance(container, dict) else entry.get("name", config["default"])

    os.makedirs(models_dir, exist_ok=True)
    for field in ("gguf", "calibration"):
        if not entry.get(field):
            continue
        filename = os.path.basename(entry[field])
        print(f"Fetching {filename} from {HF_REPO} (cached after the first run)", flush=True)
        entry[field] = os.path.abspath(hf_hub_download(HF_REPO, filename, local_dir=models_dir))

    with open(out_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"Wrote {out_path}:\n{json.dumps(config, indent=2)}")


if __name__ == "__main__":
    main()
