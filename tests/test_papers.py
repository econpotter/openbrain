"""Offline tests for openbrain.papers — the thin adapter over `citer`.

citer itself is exercised by its own suite; here we mock citer's network-facing
functions and check only openbrain's glue: Paper->meta translation, local-row
scoring, descriptor threshold handling, topic dedupe passthrough, and citation
neighbor merge. No live network is touched.
"""

import unittest
from unittest.mock import patch

from citer import AuthorRef, Paper, Venue

from openbrain import papers


class PaperToMetaTests(unittest.TestCase):
    def test_maps_all_fields(self):
        p = Paper(
            doi="10.1/x",
            arxiv_id="2103.00001",
            openalex_id="W42",
            title="A Study",
            abstract="Body.",
            year=2020,
            authors=[AuthorRef(name="Jane Doe"), AuthorRef(name="John Smith")],
            venue=Venue(name="AER", id="S1", issn="1234-5678"),
            topics=["econ", "policy"],
        )
        meta = papers.paper_to_meta(p)
        self.assertEqual(meta["title"], "A Study")
        self.assertEqual(meta["authors"], ["Jane Doe", "John Smith"])
        self.assertEqual(meta["year"], 2020)
        self.assertEqual(meta["source"], "AER")
        self.assertEqual(meta["doi"], "10.1/x")
        self.assertEqual(meta["arxiv_id"], "2103.00001")
        self.assertEqual(meta["issn_l"], "1234-5678")
        self.assertEqual(meta["venue_source_id"], "S1")
        self.assertEqual(meta["concepts"], ["econ", "policy"])
        self.assertEqual(meta["openalex_id"], "W42")
        self.assertIn("@article", meta["bibtex"])

    def test_none_passthrough(self):
        self.assertIsNone(papers.paper_to_meta(None))

    def test_venueless_paper_has_null_venue_fields(self):
        meta = papers.paper_to_meta(Paper(title="T", doi="10.1/x"))
        self.assertIsNone(meta["source"])
        self.assertIsNone(meta["issn_l"])
        self.assertIsNone(meta["venue_source_id"])


class ScoreRowTests(unittest.TestCase):
    def test_near_title_scores_above_threshold(self):
        row = {"title": "Optimal Bandwidth Choice for Regressions", "year": 2012}
        score = papers.score_row("Optimal Bandwidth Choice for Regressions", row)
        self.assertGreaterEqual(score, papers.MATCH_THRESHOLD)

    def test_unrelated_title_scores_low(self):
        row = {"title": "Land Speculation and Development"}
        score = papers.score_row("Optimal Bandwidth Choice", row)
        self.assertLess(score, papers.MATCH_THRESHOLD)

    def test_author_corroboration_ranks_matching_row_higher(self):
        matching = {"title": "Shared Title", "authors": ["Jane Doe"]}
        other = {"title": "Shared Title", "authors": ["John Smith"]}
        s_match = papers.score_row("Shared Title", matching, authors=["Doe"])
        s_other = papers.score_row("Shared Title", other, authors=["Doe"])
        self.assertGreaterEqual(s_match, s_other)


class ResolveKnownItemTests(unittest.TestCase):
    def test_doi_calls_fetch_and_returns_meta(self):
        paper = Paper(doi="10.1/x", title="A Paper", authors=[AuthorRef(name="Ada")])
        with patch.object(papers.citer, "fetch", return_value=paper) as fetch:
            meta = papers.resolve_known_item("10.1/x")
        self.assertEqual(meta["doi"], "10.1/x")
        self.assertEqual(meta["title"], "A Paper")
        # A DOI resolves without a fuzzy title search.
        self.assertTrue(fetch.called)

    def test_title_below_min_score_returns_none(self):
        weak = Paper(title="Totally Different Paper", doi="10.1/z")
        with (
            patch.object(papers.citer, "search", return_value=[weak]),
            patch.object(papers.citer, "fetch") as fetch,
        ):
            meta = papers.resolve_known_item("Some Precise Title", min_score=0.9)
        self.assertIsNone(meta)
        fetch.assert_not_called()  # never fetched: nothing cleared the threshold

    def test_title_clears_min_score_then_fetches_merged(self):
        hit = Paper(title="Some Precise Title", doi="10.1/x")
        merged = Paper(
            title="Some Precise Title", doi="10.1/x", abstract="Full abstract."
        )
        with (
            patch.object(papers.citer, "search", return_value=[hit]),
            patch.object(papers.citer, "fetch", return_value=merged) as fetch,
        ):
            meta = papers.resolve_known_item("Some Precise Title", min_score=0.7)
        self.assertEqual(meta["abstract"], "Full abstract.")
        # Fetched by the resolved DOI to merge/enrich across sources.
        resolved_ref = fetch.call_args.args[0]
        self.assertEqual(resolved_ref.doi, "10.1/x")

    def test_untitled_result_treated_as_not_found(self):
        with patch.object(papers.citer, "fetch", return_value=Paper(doi="10.1/x")):
            self.assertIsNone(papers.resolve_known_item("10.1/x"))


class SearchTopicTests(unittest.TestCase):
    def test_maps_candidates(self):
        results = [
            Paper(title="P", doi="10.1/x"),
            Paper(title="Q", doi="10.2/y"),
        ]
        with patch.object(papers.citer, "search", return_value=results):
            out = papers.search_topic("water policy", limit=5)
        self.assertEqual([m["title"] for m in out], ["P", "Q"])


class CitationNeighborsTests(unittest.TestCase):
    def test_merges_cites_and_cited_by_deduped(self):
        cites = [Paper(title="Ref", doi="10.1/ref")]
        cited_by = [
            Paper(title="Ref", doi="10.1/ref"),  # duplicate of the reference
            Paper(title="Citing", doi="10.1/cit"),
        ]
        with (
            patch.object(papers.citer, "cites", return_value=cites),
            patch.object(papers.citer, "cited_by", return_value=cited_by),
        ):
            out = papers.citation_neighbors(openalex_id="W1")
        self.assertEqual([m["doi"] for m in out], ["10.1/ref", "10.1/cit"])

    def test_unresolvable_descriptor_returns_empty(self):
        with (
            patch.object(papers.citer, "cites", return_value=[]),
            patch.object(papers.citer, "cited_by", return_value=[]),
        ):
            out = papers.citation_neighbors(title="Water Rights Allocation", year=2020)
        self.assertEqual(out, [])


class ConceptsTests(unittest.TestCase):
    def test_filters_to_openalex_source(self):
        from citer import Topic

        topics = [
            Topic(source="openalex", name="Water Economics", id="T1", works_count=100),
            Topic(source="semanticscholar", name="Economics", code="Economics"),
        ]
        with patch.object(papers.citer, "topics", return_value=topics):
            out = papers.concepts("water")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "Water Economics")
        self.assertEqual(out[0]["works_count"], 100)


if __name__ == "__main__":
    unittest.main()
