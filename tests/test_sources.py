import unittest
from unittest.mock import patch

from openbrain import sources


class IdentifierDetectionTests(unittest.TestCase):
    def test_detects_doi(self):
        self.assertEqual(sources.detect_identifier_kind("10.1257/aer.20181169"), "doi")

    def test_detects_arxiv(self):
        self.assertEqual(sources.detect_identifier_kind("arXiv:2103.00001"), "arxiv")
        self.assertEqual(sources.detect_identifier_kind("2103.00001"), "arxiv")

    def test_falls_back_to_title(self):
        self.assertEqual(
            sources.detect_identifier_kind("optimal bandwidth choice"), "title"
        )


class OpenAlexParseTests(unittest.TestCase):
    def test_reconstructs_abstract_from_inverted_index(self):
        inverted = {"Causal": [0], "inference": [1], "matters": [2]}
        self.assertEqual(
            sources._openalex_abstract(inverted), "Causal inference matters"
        )

    def test_record_maps_fields_and_strips_doi_prefix(self):
        work = {
            "display_name": "A Paper",
            "publication_year": 2024,
            "doi": "https://doi.org/10.1/x",
            "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
            "primary_location": {"source": {"display_name": "Journal X"}},
            "abstract_inverted_index": {"Hello": [0]},
            "id": "https://openalex.org/W1",
        }
        rec = sources._openalex_record(work)
        self.assertEqual(rec["title"], "A Paper")
        self.assertEqual(rec["authors"], ["Ada Lovelace"])
        self.assertEqual(rec["year"], 2024)
        self.assertEqual(rec["source"], "Journal X")
        self.assertEqual(rec["doi"], "10.1/x")
        self.assertEqual(rec["abstract"], "Hello")
        self.assertEqual(rec["source_name"], "openalex")


class CleanAbstractTests(unittest.TestCase):
    def test_strips_jats_tags_and_whitespace(self):
        raw = "<jats:p>Hello   world</jats:p>\n  again"
        self.assertEqual(sources._clean_abstract(raw), "Hello world again")

    def test_none_passthrough(self):
        self.assertIsNone(sources._clean_abstract(None))


class ResolveTests(unittest.TestCase):
    def test_lookup_merges_fields_earlier_source_wins(self):
        # openalex supplies abstract; crossref supplies bibtex + source.
        oa = sources.PaperMeta(
            title="A Paper",
            authors=["Ada"],
            year=2024,
            source=None,
            abstract="The abstract.",
            doi="10.1/x",
            bibtex=None,
            source_name="openalex",
        )
        cr = sources.PaperMeta(
            title="A Paper",
            authors=["Ada"],
            year=2024,
            source="Journal X",
            abstract=None,
            doi="10.1/x",
            bibtex="@article{...}",
            source_name="crossref",
        )
        registry = {
            "openalex": (lambda i, k: oa, lambda q, n: []),
            "crossref": (lambda i, k: cr, lambda q, n: []),
        }
        with patch.object(sources, "_REGISTRY", registry):
            merged = sources.resolve(
                identifier="10.1/x", order=["openalex", "crossref"]
            )
        self.assertEqual(merged["abstract"], "The abstract.")
        self.assertEqual(merged["source"], "Journal X")
        self.assertEqual(merged["bibtex"], "@article{...}")
        self.assertEqual(merged["source_name"], "openalex+crossref")

    def test_topic_dedups_by_doi_across_sources(self):
        dup = sources.PaperMeta(title="P", doi="10.1/x", source_name="openalex")
        same = sources.PaperMeta(title="P", doi="10.1/x", source_name="crossref")
        other = sources.PaperMeta(title="Q", doi="10.2/y", source_name="crossref")
        registry = {
            "openalex": (lambda i, k: None, lambda q, n: [dup]),
            "crossref": (lambda i, k: None, lambda q, n: [same, other]),
        }
        with patch.object(sources, "_REGISTRY", registry):
            results = sources.resolve(topic="x", order=["openalex", "crossref"])
        self.assertEqual([r["doi"] for r in results], ["10.1/x", "10.2/y"])

    def test_lookup_returns_none_when_no_source_has_title(self):
        registry = {"openalex": (lambda i, k: None, lambda q, n: [])}
        with patch.object(sources, "_REGISTRY", registry):
            self.assertIsNone(sources.resolve(identifier="10.1/x", order=["openalex"]))


if __name__ == "__main__":
    unittest.main()
