import json
import subprocess
import sys

from agent_commons import security


def test_production_scrypt_cost_is_unchanged():
    # The suite lowers the cost in-process; a fresh interpreter sees the real defaults.
    code = (
        "import json; from agent_commons import security as s; "
        "print(json.dumps([s.SCRYPT_N, s.SCRYPT_R, s.SCRYPT_P, s.hash_password('x')]))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    n, r, p, encoded = json.loads(out.stdout)
    assert (n, r, p) == (32768, 8, 3)
    assert encoded.startswith("scrypt$32768$8$3$")
    assert security.verify_password("x", encoded) is False  # other cost: never accepted here


def test_tests_use_cheap_hashes_that_verify_consistently():
    assert security.SCRYPT_N < 32768
    encoded = security.hash_password("a sufficiently long password")
    assert encoded.startswith(f"scrypt${security.SCRYPT_N}${security.SCRYPT_R}$")
    assert security.verify_password("a sufficiently long password", encoded)
    assert not security.verify_password("another password", encoded)
    assert not security.verify_password("anything", None)  # unknown identity never validates
    assert security.verify_password("dummy password for unknown identity", None) is False
