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


if __name__ == "__main__":
    unittest.main()
