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


class AcquireFieldCaptureTests(unittest.TestCase):
    def test_openalex_record_captures_venue_keys_and_concepts(self):
        work = {
            "display_name": "Water demand",
            "publication_year": 2021,
            "authorships": [],
            "primary_location": {
                "source": {
                    "display_name": "American Journal of Agricultural Economics",
                    "issn_l": "0002-9092",
                    "id": "https://openalex.org/S123",
                }
            },
            "concepts": [{"display_name": "Water"}, {"display_name": "Demand"}],
            "abstract_inverted_index": None,
            "doi": None,
        }
        rec = sources._openalex_record(work)
        self.assertEqual(rec["issn_l"], "0002-9092")
        self.assertEqual(rec["venue_source_id"], "S123")
        self.assertEqual(rec["concepts"], ["Water", "Demand"])

    def test_citation_neighbors_merges_and_dedups(self):
        def fake_get_json(url, headers=None):
            if "works/W1" in url:
                return {
                    "referenced_works": ["https://openalex.org/W2"],
                    "cited_by_api_url": "https://api.openalex.org/works?filter=cites:W1",
                }
            if "filter=openalex_id:W2" in url:
                return {
                    "results": [
                        {"display_name": "Ref Paper", "doi": "https://doi.org/10.1/ref"}
                    ]
                }
            if "cites:W1" in url:
                return {
                    "results": [
                        {"display_name": "Citing", "doi": "https://doi.org/10.1/cite"},
                        # Same DOI as the referenced work -> deduplicated away.
                        {
                            "display_name": "Ref again",
                            "doi": "https://doi.org/10.1/ref",
                        },
                    ]
                }
            return None

        with patch.object(sources, "_get_json", fake_get_json):
            neighbors = sources.openalex_citation_neighbors("W1")
        self.assertEqual([n["doi"] for n in neighbors], ["10.1/ref", "10.1/cite"])

    def test_citation_neighbors_returns_empty_when_work_missing(self):
        with patch.object(sources, "_get_json", lambda *a, **k: None):
            self.assertEqual(sources.openalex_citation_neighbors("W404"), [])

    def test_crossref_record_captures_subject_and_issn(self):
        item = {
            "title": ["X"],
            "author": [],
            "container-title": ["AJAE"],
            "subject": ["Economics", "Water"],
            "ISSN": ["0002-9092"],
        }
        rec = sources._crossref_record(item)
        self.assertEqual(rec["concepts"], ["Economics", "Water"])
        self.assertEqual(rec["issn_l"], "0002-9092")


class PagedSearchTests(unittest.TestCase):
    def _page(self, names, next_cursor):
        return {
            "results": [
                {
                    "display_name": n,
                    "authorships": [],
                    "primary_location": {},
                    "concepts": [],
                    "abstract_inverted_index": None,
                    "doi": None,
                }
                for n in names
            ],
            "meta": {"next_cursor": next_cursor},
        }

    def test_paginates_and_flags_truncation(self):
        pages = [self._page(["p0", "p1"], "C2"), self._page(["p2"], None)]
        calls = {"i": 0}

        def fake_get_json(url, headers=None):
            out = pages[calls["i"]]
            calls["i"] += 1
            return out

        with patch.object(sources, "_get_json", fake_get_json):
            recs, truncated = sources.openalex_search_paged(
                "water", limit=2, per_page=2
            )
        self.assertEqual(len(recs), 2)
        self.assertTrue(truncated)

    def test_pushes_filters_into_url(self):
        seen = {}

        def fake_get_json(url, headers=None):
            seen["url"] = url
            return self._page([], None)

        with patch.object(sources, "_get_json", fake_get_json):
            sources.openalex_search_paged(
                "water", issns=["0002-9092", "0043-1397"], since=2015, limit=5
            )
        self.assertIn("primary_location.source.issn", seen["url"])
        self.assertIn("from_publication_date", seen["url"])


class RetryTests(unittest.TestCase):
    def test_with_retry_succeeds_after_transient_failures(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("429")
            return "ok"

        with patch.object(sources.time, "sleep", lambda *_: None):
            self.assertEqual(sources._with_retry(flaky, attempts=3), "ok")
        self.assertEqual(calls["n"], 3)

    def test_with_retry_reraises_after_exhaustion(self):
        def always_fail():
            raise RuntimeError("boom")

        with patch.object(sources.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError):
                sources._with_retry(always_fail, attempts=2)
