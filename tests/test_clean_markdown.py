import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clean_markdown import DEFAULT_CONFIG, _deep_merge, transform_text  # noqa: E402


class CleanerTests(unittest.TestCase):
    def config(self):
        return _deep_merge(DEFAULT_CONFIG, {})

    def test_html_table_to_markdown_preserves_duplicates(self):
        source = (
            "# T\n\n<table><tr><td>#</td><td>Name</td></tr>"
            "<tr><td>6</td><td>sine</td></tr>"
            "<tr><td>6</td><td>sin</td></tr></table>\n"
        )
        result = transform_text(source, self.config(), source_name="fixture")
        self.assertIn("| # | Name |", result.text)
        self.assertIn("| 6 | sine |", result.text)
        self.assertEqual(result.tables[0]["duplicate_first_column_values"], ["6"])
        self.assertNotIn("<table", result.text)

    def test_rowspan_is_expanded(self):
        source = (
            "# T\n\n<table><tr><td>A</td><td>B</td></tr>"
            '<tr><td rowspan="2">x</td><td>1</td></tr>'
            "<tr><td>2</td></tr></table>\n"
        )
        result = transform_text(source, self.config(), source_name="fixture")
        self.assertIn("| x | 1 |", result.text)
        self.assertIn("| x | 2 |", result.text)

    def test_image_and_caption_are_audited_and_removed(self):
        source = "# T\n\n![](images/a.jpg)  \nFigure 1.2: Schematic\n\nBody\n"
        result = transform_text(source, self.config(), source_name="fixture")
        self.assertNotIn("images/a.jpg", result.text)
        self.assertNotIn("Figure 1.2:", result.text)
        self.assertEqual(result.images[0]["caption"], "Figure 1.2: Schematic")

    def test_code_content_is_protected_while_language_is_removed(self):
        source = "# T\n\n```yaml\n• literal code  \n```\n\n• prose\n"
        result = transform_text(source, self.config(), source_name="fixture")
        self.assertIn("```\n• literal code  \n```", result.text)
        self.assertIn("- prose", result.text)

    def test_toc_heading_and_idempotence(self):
        source = (
            "# Book\n\n## Contents\n1 Intro 3\n\n## Prefaces\nP\n\n"
            "## Chapter 1\n\n## Introduction\nText\n\n## 1.1 Topic\nBody\n"
        )
        result = transform_text(source, self.config(), source_name="fixture")
        self.assertNotIn("1 Intro 3", result.text)
        self.assertIn("# 1 Introduction", result.text)
        self.assertIn("## 1.1 Topic", result.text)
        self.assertTrue(result.report["quality"]["idempotent"])

    def test_unmarked_numbered_heading_is_promoted(self):
        source = "# T\n\n2.3.4 Device parameters\n\nBody\n"
        result = transform_text(source, self.config(), source_name="fixture")
        self.assertIn("### 2.3.4 Device parameters", result.text)

    def test_empty_spacer_header_is_warning_not_quarantine(self):
        source = (
            "# T\n\n<table><tr><td>A</td><td></td><td>B</td></tr>"
            "<tr><td>1</td><td></td><td>2</td></tr></table>\n"
        )
        result = transform_text(source, self.config(), source_name="fixture")
        self.assertIn("Column 2", result.text)
        self.assertEqual(result.quarantined_tables, [])

    def test_overextended_code_block_is_flattened_without_content_loss(self):
        source = (
            "# T\n\n```yaml\ncommand one\n\n2.3.4 Device parameters\n\n"
            "Technical prose\n```\n"
        )
        result = transform_text(source, self.config(), source_name="fixture")
        self.assertIn("### 2.3.4 Device parameters", result.text)
        self.assertIn("command one", result.text)
        self.assertIn("Technical prose", result.text)
        self.assertNotIn("```", result.text)


if __name__ == "__main__":
    unittest.main()
