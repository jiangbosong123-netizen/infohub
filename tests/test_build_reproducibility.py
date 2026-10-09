import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _name(requirement: str) -> str:
    """PEP 503 normalized project name, without extras, versions or markers."""
    name = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement).group(1)
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked() -> dict[str, list[str]]:
    """Locked packages in requirements.txt and their hashes."""
    packages: dict[str, list[str]] = {}
    current = None
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        pinned = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==\S+", line)
        if pinned:
            current = _name(pinned.group(1))
            packages[current] = []
        elif current and "--hash=sha256:" in line:
            packages[current].append(line.split("--hash=sha256:")[1].split()[0])
    return packages


class BuildReproducibilityTests(unittest.TestCase):
    def test_every_direct_dependency_is_locked_with_hashes(self):
        locked = _locked()
        direct = [_name(line) for line in (ROOT / "requirements.in").read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual([name for name in direct if name not in locked], [])
        self.assertEqual([name for name, hashes in locked.items() if not hashes], [])
        for hashes in locked.values():
            for value in hashes:
                self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_the_image_installs_the_lock_on_a_pinned_base(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        bases = re.findall(r"^FROM (\S+)", dockerfile, re.M)
        self.assertEqual(len(bases), 2)
        self.assertEqual(len(set(bases)), 1)
        self.assertRegex(bases[0], r"^python:3\.12-slim@sha256:[0-9a-f]{64}$")
        install = dockerfile.index("--require-hashes -r requirements.txt")
        # The version comes after the dependencies, so a new version reuses the installed layer.
        self.assertGreater(dockerfile.index("ARG APP_VERSION"), install)
        self.assertNotIn("COPY . .", dockerfile)

    def test_ci_installs_the_same_lock(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("pip install --require-hashes -r requirements.txt", workflow)
        self.assertIn('docker run --rm -v "$PWD:/src" -w /src "infohub:$APP_VERSION"', workflow)


if __name__ == "__main__":
    unittest.main()
