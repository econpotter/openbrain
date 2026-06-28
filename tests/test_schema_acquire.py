import unittest
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "core"


class SchemaAcquireTests(unittest.TestCase):
    def test_baseline_files_present(self):
        self.assertTrue((SCHEMA_DIR / "007_venues.sql").exists())
        self.assertTrue((SCHEMA_DIR / "008_saved_queries.sql").exists())

    def test_library_has_new_columns(self):
        text = (SCHEMA_DIR / "003_library.sql").read_text()
        for col in ("venue_abbrev", "venue_source_id", "issn", "concepts"):
            self.assertIn(col, text, f"missing column {col}")

    def test_library_has_venue_indexes(self):
        text = (SCHEMA_DIR / "003_library.sql").read_text()
        self.assertIn("library_venue_abbrev_idx", text)
        self.assertIn("library_concepts_idx", text)

    def test_venues_and_saved_queries_define_tables(self):
        self.assertIn(
            "CREATE TABLE venues", (SCHEMA_DIR / "007_venues.sql").read_text()
        )
        self.assertIn(
            "CREATE TABLE saved_queries",
            (SCHEMA_DIR / "008_saved_queries.sql").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
