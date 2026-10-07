import unittest

from app.agreement_stats import categorical_agreement, empty_confusion


class AgreementStatsTests(unittest.TestCase):
    def test_no_pairs_reports_nothing(self):
        result = categorical_agreement(empty_confusion(("a", "b")), 0)
        self.assertEqual((result.observed_agreement, result.expected_agreement, result.cohen_kappa),
                         (None, None, None))

    def test_single_shared_class_is_undefined_not_perfect(self):
        confusion = empty_confusion(("a", "b"))
        confusion["a"]["a"] = 5
        result = categorical_agreement(confusion, 5)
        self.assertEqual((result.observed_agreement, result.expected_agreement), (1.0, 1.0))
        self.assertIsNone(result.cohen_kappa)

    def test_known_values(self):
        perfect = empty_confusion(("a", "b"))
        perfect["a"]["a"], perfect["b"]["b"] = 3, 2
        self.assertAlmostEqual(categorical_agreement(perfect, 5).cohen_kappa, 1.0)
        opposed = empty_confusion(("a", "b"))
        opposed["a"]["b"], opposed["b"]["a"] = 2, 2
        self.assertAlmostEqual(categorical_agreement(opposed, 4).cohen_kappa, -1.0)
        mixed = empty_confusion(("positive", "negative", "neutral"))
        mixed["positive"]["positive"], mixed["negative"]["neutral"] = 1, 1
        result = categorical_agreement(mixed, 2)
        self.assertEqual(result.observed_agreement, 0.5)
        self.assertAlmostEqual(result.expected_agreement, 0.25)
        self.assertAlmostEqual(result.cohen_kappa, 1 / 3)

    def binary(self, a, b, c, d):
        confusion = empty_confusion(("relevant", "not_relevant"))
        confusion["relevant"]["relevant"], confusion["relevant"]["not_relevant"] = a, b
        confusion["not_relevant"]["relevant"], confusion["not_relevant"]["not_relevant"] = c, d
        return categorical_agreement(confusion, a + b + c + d)

    def test_specific_agreement_separates_the_rare_class(self):
        result = self.binary(10, 5, 3, 82)
        self.assertAlmostEqual(result.observed_agreement, 0.92)
        self.assertAlmostEqual(result.cohen_kappa, (0.92 - 0.759) / (1 - 0.759))
        self.assertAlmostEqual(result.specific_agreement["relevant"], 20 / 28)
        self.assertAlmostEqual(result.specific_agreement["not_relevant"], 164 / 172)

    def test_kappa_interval_is_reproducible_and_narrows_with_more_pairs(self):
        small = self.binary(10, 5, 3, 82)
        again = self.binary(10, 5, 3, 82)
        large = self.binary(40, 20, 12, 328)
        self.assertEqual(small.kappa_bootstrap_95, again.kappa_bootstrap_95)
        low, high = small.kappa_bootstrap_95
        self.assertLess(low, small.cohen_kappa)
        self.assertGreater(high, small.cohen_kappa)
        self.assertLess(large.kappa_bootstrap_95[1] - large.kappa_bootstrap_95[0], high - low)

    def test_undefined_kappa_has_no_interval(self):
        confusion = empty_confusion(("a", "b"))
        confusion["a"]["a"] = 5
        result = categorical_agreement(confusion, 5)
        self.assertIsNone(result.kappa_bootstrap_95)
        self.assertEqual(result.specific_agreement, {"a": 1.0, "b": None})


if __name__ == "__main__":
    unittest.main()
