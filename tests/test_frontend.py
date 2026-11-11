"""Run browser-script behavior checks when the optional Node.js test runtime is available."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_frontend_behaviors():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is optional; install it to run browser-script behavior checks")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("frontend.test.cjs"))],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
