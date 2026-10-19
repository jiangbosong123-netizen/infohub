import unittest

from app.web.routes import templates


def card(**fields):
    item = dict(id=1, hms="09:00", source_name="Reuters", official=False, event_label="", translated=False,
                curation_needs_review=False, via="", companies=[], score=None, url="https://example.com/a",
                title="Title", summary="", reason="", story_count=1, topics=[], ai_cat="", cluster_id=None,
                extra_sources=0)
    item.update(fields)
    return str(templates.env.get_template("macros.html").module.item_card(item))


class ItemCardTests(unittest.TestCase):
    def test_a_company_chip_shows_once_and_other_topics_stay(self):
        html = card(companies=[dict(slug="nvidia", label="英伟达")],
                    topics=[dict(slug="nvidia", name="英伟达"), dict(slug="product-launch", name="产品更新")])
        self.assertEqual(html.count('href="/topics/nvidia"'), 1)
        self.assertIn('href="/topics/product-launch"', html)


if __name__ == "__main__":
    unittest.main()
