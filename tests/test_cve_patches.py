"""Portability guards for the CVE-2026-109618/109619 backports.

The patches are applied to PHP's C sources on every platform. MSVC compiles
them as C, where `true` and `false` are not keywords, so a mitigation written
as `|| true` builds on macOS and fails on Windows with

    ext\\session\\session.c(2966): error C2065: 'true': undeclared identifier
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_ROOT = ROOT / "runtime/patch"

# Boolean literals used as expressions, not as part of an identifier.
LITERAL_EXPRESSION = re.compile(r"(\|\||&&)\s*(true|false)\b|\b(true|false)\s*(\|\||&&)")


class CvePatchPortabilityTests(unittest.TestCase):
    def test_patches_exist(self):
        patches = sorted(PATCH_ROOT.glob("php*/cve-2026-109618-109619.patch"))
        self.assertGreaterEqual(len(patches), 9, patches)

    def test_added_code_avoids_cpp_only_boolean_literals(self):
        offenders = []
        for patch in sorted(PATCH_ROOT.glob("php*/cve-2026-109618-109619.patch")):
            for number, line in enumerate(patch.read_text().splitlines(), 1):
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                if LITERAL_EXPRESSION.search(line):
                    offenders.append(f"{patch.relative_to(ROOT)}:{number}: {line}")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
