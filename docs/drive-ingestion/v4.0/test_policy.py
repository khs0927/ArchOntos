import unittest
from reference_policy import (archive_member_id, observed_coverage, to_metres,
                              minimum_check, may_use_content)


class PolicyTests(unittest.TestCase):
    def test_archive_revision_identity(self):
        self.assertNotEqual(archive_member_id("s1", 0, "A.dwg"),
                            archive_member_id("s2", 0, "A.dwg"))

    def test_archive_duplicate_names(self):
        self.assertNotEqual(archive_member_id("s1", 0, "A.dwg"),
                            archive_member_id("s1", 1, "A.dwg"))

    def test_unsafe_paths(self):
        for p in ["../a", "/etc/passwd", "C:\\a", "x/../../a", "x\x00y"]:
            with self.subTest(p=p), self.assertRaises(ValueError):
                archive_member_id("s1", 0, p)

    def test_review_and_exception_not_success(self):
        c = observed_coverage([
            {"relevance": "REVIEW_REQUIRED"},
            {"relevance": "ARCHITECTURE", "extraction": "UNSUPPORTED",
             "exception_approved": True},
            {"relevance": "ARCHITECTURE", "extraction": "COMPLETE"}])
        self.assertEqual(c["classification_resolved"], 2)
        self.assertEqual(c["review_pending"], 1)
        self.assertEqual(c["content_complete_ratio"], .5)
        self.assertIsNone(c["global_inventory_ratio"])

    def test_units(self):
        self.assertEqual(to_metres(1500, 4), 1.5)
        self.assertEqual(to_metres(1.5, 6), 1.5)
        self.assertAlmostEqual(to_metres(1, 1), .0254)
        with self.assertRaises(ValueError):
            to_metres(1500, 0)

    def check(self, lo, hi, **kwargs):
        defaults = dict(applicable=True, measurement_verified=True,
                        uncertainty_kind="DETERMINISTIC_BOUND")
        defaults.update(kwargs)
        return minimum_check(lo, hi, 1.8, **defaults)

    def test_threshold_crossing(self):
        self.assertEqual(self.check(1.79, 1.81), "NEEDS_REVIEW")

    def test_numerical_candidates(self):
        self.assertEqual(self.check(1.8, 1.81), "PASS_CANDIDATE")
        self.assertEqual(self.check(1.6, 1.7), "FAIL_CANDIDATE")

    def test_unknown_applicability(self):
        self.assertEqual(self.check(2, 2, applicable=None), "NEEDS_REVIEW")

    def test_unknown_measurement(self):
        self.assertEqual(self.check(2, 2, measurement_verified=False), "UNMEASURABLE")
        self.assertEqual(self.check(float("nan"), 2), "UNMEASURABLE")

    def test_statistical_interval_not_bound(self):
        self.assertEqual(self.check(2, 2.1, uncertainty_kind="STATISTICAL_CI"),
                         "NEEDS_REVIEW")

    def test_access_loss_and_stale_acl(self):
        self.assertTrue(may_use_content("ACCESSIBLE", True, True))
        for state, fresh, authorized in [
                ("INACCESSIBLE", True, True), ("UNKNOWN", True, True),
                ("ACCESSIBLE", False, True), ("ACCESSIBLE", True, False)]:
            self.assertFalse(may_use_content(state, fresh, authorized))


if __name__ == "__main__":
    unittest.main(verbosity=2)
