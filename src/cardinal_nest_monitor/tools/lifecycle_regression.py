"""Lifecycle regression harness.

Runs the REAL analyzer (actual Anthropic API call) on each curated reference
image in `evidence/reference/lifecycle/` and compares the returned
NestObservation against the `.expected.json` ground-truth alongside each
image. Prints a pass/fail table and exits non-zero if ANY image fails.

This is the HARD GATE before enabling `LIFECYCLE_TRACKING_ENABLED=true` in
production. If any image fails, the feature does not ship.

Cost: ~$0.02-0.03 per image × ~13 images = ~$0.30-0.50 per full run.

Usage:
  python -m cardinal_nest_monitor.tools.lifecycle_regression
  python -m cardinal_nest_monitor.tools.lifecycle_regression --verbose
  python -m cardinal_nest_monitor.tools.lifecycle_regression --image <path>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from cardinal_nest_monitor import analyzer as analyzer_mod
from cardinal_nest_monitor.schema import NestObservation


log = logging.getLogger(__name__)

_DEFAULT_LIFECYCLE_DIR = (
    Path(__file__).resolve().parents[3] / "evidence" / "reference" / "lifecycle"
)


class CheckResult:
    def __init__(self, field: str, passed: bool, detail: str = "") -> None:
        self.field = field
        self.passed = passed
        self.detail = detail

    def __repr__(self) -> str:
        status = "OK " if self.passed else "FAIL"
        return f"[{status}] {self.field} {self.detail}"


def _evaluate_expected(
    obs: NestObservation, expected: dict
) -> list[CheckResult]:
    """Compare an observation against an expected.json dict.

    The expected dict may contain any of:
      cardinal_on_nest, mother_cardinal_present, chicks_visible,
      mother_feeding_chicks, eggs_visible, threat_species_detected_empty (bool),
      chick_count_estimate_min (int), chick_count_estimate_max (int),
      confidence_min (float), confidence_max (float)

    Fields absent from expected are not checked — this lets each image
    focus on its discriminating attributes.
    """
    checks: list[CheckResult] = []

    for field in ("cardinal_on_nest", "mother_cardinal_present", "chicks_visible", "eggs_visible"):
        if field in expected:
            actual = getattr(obs, field)
            passed = actual == expected[field]
            checks.append(CheckResult(
                field, passed,
                f"expected={expected[field]!r} got={actual!r}",
            ))

    if "mother_feeding_chicks" in expected:
        passed = obs.mother_feeding_chicks == expected["mother_feeding_chicks"]
        checks.append(CheckResult(
            "mother_feeding_chicks", passed,
            f"expected={expected['mother_feeding_chicks']} got={obs.mother_feeding_chicks}",
        ))

    if "threat_species_detected_empty" in expected:
        want_empty = bool(expected["threat_species_detected_empty"])
        actual_empty = len(obs.threat_species_detected) == 0
        passed = want_empty == actual_empty
        checks.append(CheckResult(
            "threat_species_detected", passed,
            f"expected_empty={want_empty} got={obs.threat_species_detected}",
        ))

    if "chick_count_estimate_min" in expected or "chick_count_estimate_max" in expected:
        count = obs.chick_count_estimate
        lo = expected.get("chick_count_estimate_min")
        hi = expected.get("chick_count_estimate_max")
        if count is None:
            passed = False
            detail = f"expected in [{lo}, {hi}] got=None"
        else:
            passed = (lo is None or count >= lo) and (hi is None or count <= hi)
            detail = f"expected in [{lo}, {hi}] got={count}"
        checks.append(CheckResult("chick_count_estimate", passed, detail))

    if "confidence_min" in expected:
        want = float(expected["confidence_min"])
        passed = obs.confidence >= want
        checks.append(CheckResult(
            "confidence_min", passed,
            f"expected >= {want:.2f} got={obs.confidence:.2f}",
        ))

    if "confidence_max" in expected:
        want = float(expected["confidence_max"])
        passed = obs.confidence <= want
        checks.append(CheckResult(
            "confidence_max", passed,
            f"expected <= {want:.2f} got={obs.confidence:.2f}",
        ))

    return checks


async def _run_one(
    image_path: Path, expected: dict, verbose: bool = False
) -> tuple[bool, NestObservation, list[CheckResult]]:
    jpeg = image_path.read_bytes()
    obs = await analyzer_mod.analyze(jpeg)
    checks = _evaluate_expected(obs, expected)
    passed = all(c.passed for c in checks)
    if verbose:
        print(f"\n  Observation: cardinal_on_nest={obs.cardinal_on_nest} "
              f"chicks_visible={obs.chicks_visible} "
              f"chick_count={obs.chick_count_estimate} "
              f"feeding={obs.mother_feeding_chicks} "
              f"conf={obs.confidence:.2f}")
        print(f"  Summary: {obs.summary}")
    return passed, obs, checks


async def run(lifecycle_dir: Path, verbose: bool = False, only: str | None = None) -> int:
    """Run the regression suite. Returns exit code (0 = all passed)."""
    images = sorted(p for p in lifecycle_dir.glob("*.jpg"))
    if only:
        images = [p for p in images if only in p.name]
    if not images:
        print(f"No images found in {lifecycle_dir}")
        return 2

    print(f"Running lifecycle regression on {len(images)} image(s)")
    print(f"Lifecycle dir: {lifecycle_dir}\n")

    total_passed = 0
    total_failed = 0
    failed_images: list[str] = []

    for img_path in images:
        expected_path = img_path.with_suffix(".expected.json")
        if not expected_path.exists():
            print(f"[SKIP] {img_path.name}: no .expected.json sibling")
            continue
        expected = json.loads(expected_path.read_text())
        try:
            passed, obs, checks = await _run_one(img_path, expected, verbose)
        except Exception as e:
            print(f"[ERROR] {img_path.name}: {type(e).__name__}: {e}")
            total_failed += 1
            failed_images.append(img_path.name)
            continue

        status = "PASS" if passed else "FAIL"
        stage = expected.get("stage", "?")
        print(f"[{status}] {img_path.name} ({stage})")
        for c in checks:
            symbol = "  ✓" if c.passed else "  ✗"
            print(f"{symbol} {c.field}: {c.detail}")

        if passed:
            total_passed += 1
        else:
            total_failed += 1
            failed_images.append(img_path.name)

    print()
    print("=" * 60)
    print(f"Results: {total_passed}/{total_passed + total_failed} images passed")
    if failed_images:
        print("Failures:")
        for name in failed_images:
            print(f"  - {name}")
        print("\nDO NOT enable LIFECYCLE_TRACKING_ENABLED until all images pass.")
        return 1
    print("ALL PASS — safe to enable LIFECYCLE_TRACKING_ENABLED=true")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir", type=Path, default=_DEFAULT_LIFECYCLE_DIR,
        help="Directory containing reference images + .expected.json",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--image", "-i", type=str, default=None,
        help="Only run images whose filename contains this substring",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(run(args.dir, args.verbose, args.image))


if __name__ == "__main__":
    sys.exit(main())
