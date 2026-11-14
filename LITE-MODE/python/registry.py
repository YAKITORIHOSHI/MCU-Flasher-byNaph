"""Fixed PlatformIO library search, run in an owned cancellable tool process."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

MAX_RESPONSE = 2 * 1024 * 1024


def search(query):
    parameters = urllib.parse.urlencode({"query": "type:library " + query[:200],
        "limit": 100, "page": 1, "sort": "relevance"})
    url = "https://api.registry.platformio.org/v3/search?" + parameters
    with urllib.request.urlopen(url, timeout=20) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("Registry response exceeds the 2 MiB budget")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
        raise ValueError("Registry returned an invalid library list")
    results = []
    for item in payload.get("items", [])[:100]:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner") or {}
        version = item.get("version") or {}
        owner = str(owner.get("username", "")) if isinstance(owner, dict) else ""
        version = str(version.get("name", "")) if isinstance(version, dict) else ""
        name = str(item.get("name", ""))
        # Keep the response, protocol frame and buttons bounded independently
        # of how the registry encodes its optional metadata.
        if max(len(owner), len(name), len(version)) > 200:
            continue
        results.append({"name": name, "owner": owner, "version": version,
            "description": str(item.get("description") or "")[:500],
            "spec": f"{owner}/{name}@{version}"})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    state = Path(os.environ["MCU_LITE_STATE"]).resolve(strict=True)
    result = args.result.resolve()
    if not result.is_relative_to(state):
        raise RuntimeError("Registry search writes only private Lite state")
    from isolation import protect_original
    protect_original(Path(__file__).resolve().parents[2])
    results = search(args.query)
    scratch = result.with_suffix(".tmp")
    scratch.write_text(json.dumps(results), encoding="utf-8")
    scratch.replace(result)


if __name__ == "__main__":
    main()
