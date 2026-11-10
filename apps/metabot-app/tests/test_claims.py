"""G2: every claim on the claims page names its proof, and the proof exists: a unit
test (``tests/<file>.py::<test>``), a self-test journey step (``selftest:<journey>/<step>``),
a tablet-walk step (``tablet-e2e:<journey>``) or a page suite check (``ui:<file>``).
A claim with no proof, or a proof that names nothing, fails here before it ships."""

import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
CLAIMS = APP / "docs" / "CLAIMS.md"
ROW = re.compile(r"^\|\s*(?P<claim>[^|]+?)\s*\|\s*(?P<proof>[^|]+?)\s*\|\s*$", re.M)


def rows():
    out = []
    for m in ROW.finditer(CLAIMS.read_text(encoding="utf-8")):
        claim, proof = m.group("claim"), m.group("proof")
        if claim.lower() in ("claim", "---") or set(claim) <= {"-", " "}:
            continue
        out.append(
            (claim, [p.strip().strip("`") for p in proof.split(";") if p.strip()])
        )
    return out


class ClaimsTests(unittest.TestCase):
    def test_the_page_exists_and_has_claims(self):
        self.assertTrue(CLAIMS.is_file(), "docs/CLAIMS.md is the claims page")
        self.assertGreaterEqual(len(rows()), 8)

    def test_every_claim_names_at_least_one_proof(self):
        for claim, proofs in rows():
            self.assertTrue(proofs, f"no proof for: {claim}")

    def test_every_proof_names_something_that_exists(self):
        journeys = APP / "src" / "metabot_app" / "selftest" / "journeys"
        for claim, proofs in rows():
            for proof in proofs:
                with self.subTest(claim=claim, proof=proof):
                    if proof.startswith("tests/"):
                        file, _, name = proof.partition("::")
                        text = (APP / file).read_text(encoding="utf-8")
                        self.assertTrue(f"def {name}(" in text, f"{file} has no {name}")
                    elif proof.startswith("selftest:"):
                        journey, _, step = proof[len("selftest:") :].partition("/")
                        text = (journeys / f"{journey}.py").read_text(encoding="utf-8")
                        self.assertTrue(
                            f'"{step}"' in text,
                            f"journey {journey} has no step {step!r}",
                        )
                    elif proof.startswith("tablet-e2e:"):
                        text = (APP / "tools" / "tablet-e2e.sh").read_text(
                            encoding="utf-8"
                        )
                        self.assertTrue(
                            f'{proof[len("tablet-e2e:") :]}"' in text,
                            f"the tablet walk has no step {proof}",
                        )
                    elif proof.startswith("ui:"):
                        self.assertTrue(
                            (APP / "tests" / proof[len("ui:") :]).is_file(), proof
                        )
                    else:
                        self.fail(f"unknown proof kind: {proof}")


if __name__ == "__main__":
    unittest.main()
