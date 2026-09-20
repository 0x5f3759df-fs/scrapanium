import os
from pathlib import Path
import subprocess
import sys


def test_exact_binary_comparison_and_returned_ownership():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    binary = build(root / "tests/bytes_equal.bend", root / "build/bytes-equal", True)
    for threads in (1, 4):
        out = subprocess.run([str(binary), "--threads", str(threads)], env=os.environ,
                             capture_output=True, text=True, timeout=10)
        assert out.returncode == 0, out.stdout + out.stderr
        assert out.stdout.splitlines() == ["equal", "different", "equal", "different", "different", "different", "different"]
