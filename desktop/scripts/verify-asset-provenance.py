"""Offline checks for recovered xterm provenance and staged release notices.

This verifies fixed repository artifacts only; it downloads nothing and does not
certify Monaco, codicon, dependency licensing, installers or public release.
"""
from pathlib import Path
import hashlib
import json


ROOT = Path(__file__).resolve().parents[2]


def checked_file(relative, base=ROOT):
    path = base / relative
    resolved = path.resolve(strict=True)
    assert resolved.is_relative_to(base.resolve()), relative
    assert resolved.is_file(), relative
    return resolved


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    evidence = json.loads((ROOT / "audit/migration/xterm-provenance.json").read_text())
    assert evidence["schemaVersion"] == 1
    assert [(p["package"], p["version"]) for p in evidence["packages"]] == [
        ("xterm", "5.3.0"), ("xterm-addon-fit", "0.8.0")]
    notices = set()
    checks = []
    for package in evidence["packages"]:
        notice = package["notice"]
        assert digest(checked_file(notice["path"])) == notice["sha256"] == package["licenseSha256"]
        notices.add(notice["path"])
        checks.append(f"{package['package']}-matching-archive-license")
        for entry in package["files"]:
            assert digest(checked_file(entry["local"])) == entry["localSha256"]
            if entry["byteIdentical"]:
                assert entry["archiveSha256"] == entry["localSha256"]
                checks.append(f"{entry['local']}-matches-pinned-archive")
            else:
                assert entry["local"] == "src/assets/xterm/xterm.css"
                original_notice = entry["derivation"]
                assert digest(checked_file(original_notice["upstreamNotice"])) == original_notice["upstreamNoticeSha256"]
                notices.add(original_notice["upstreamNotice"])
                checks.append("existing-css-and-recovered-original-copyright-block")
    manifest = json.loads((ROOT / "desktop/ui/public/vendor/manifest.json").read_text())
    seen = set()
    staged_notices = set()
    for entry in manifest["files"]:
        assert entry["destination"] not in seen, entry["destination"]
        seen.add(entry["destination"])
        source = checked_file(entry["source"])
        destination = checked_file(entry["destination"], ROOT / "desktop/ui/public")
        assert digest(source) == digest(destination) == entry["sha256"], entry["destination"]
        assert source.stat().st_size == destination.stat().st_size == entry["bytes"]
        if entry.get("role") == "matching-upstream-notice":
            assert entry["source"] in notices, entry
            staged_notices.add(entry["source"])
    assert staged_notices == notices and len(notices) == 3
    checks.append("all-three-matching-notices-present-in-staged-web-assets")
    checks.append("every-staged-artifact-matches-source-size-and-sha256")
    print(json.dumps(dict(schemaVersion=1, passed=True, checks=checks,
                          checksPassed=len(checks), stagedArtifacts=len(seen),
                          matchingNotices=len(notices), networkAccess=False,
                          fullReleaseProvenanceQualified=False)))


if __name__ == "__main__":
    main()
