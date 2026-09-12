"""One-command validation for the 6363 work-machine checkout.

Examples:
  python workstation_test.py                 # software tests
  python workstation_test.py --hardware      # tests plus live HTTP checks
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.parse
import urllib.request
import unittest


def run_unit_tests() -> bool:
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.discover("tests")
    )
    return result.wasSuccessful()


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def run_hardware_checks(base_url: str, device: str) -> bool:
    checks: list[tuple[str, bool, object]] = []
    health = get_json(base_url + "/health")
    checks.append(("server health", bool(health), health))
    status = get_json(base_url + "/api/ai/unified/status")
    checks.append(("unified status readable", isinstance(status, dict), status))
    for terminal, label in (("PFI0", "PFI0 counter"), ("PFI1", "PFI1 counter")):
        query = urllib.parse.urlencode({"device": device, "terminal": terminal, "duration": "0.1"})
        try:
            value = get_json(base_url + "/api/pfi/count?" + query)
            checks.append((label, isinstance(value, dict) and "count" in value, value))
        except Exception as exc:
            checks.append((label, False, str(exc)))
    for label, ok, value in checks:
        print(f"[{ 'PASS' if ok else 'FAIL' }] {label}: {value}")
    return all(ok for _, ok, _ in checks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hardware", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--device", default="Dev2")
    args = parser.parse_args()
    ok = run_unit_tests()
    if args.hardware:
        try:
            ok = run_hardware_checks(args.base_url, args.device) and ok
        except Exception as exc:
            print(f"[FAIL] hardware checks: {exc}")
            ok = False
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
