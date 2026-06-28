import unittest

from openbrain import venues


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _VenueConnection:
    """In-memory stand-in for the venues table."""

    def __init__(self):
        self.rows = {}  # abbrev -> dict

    async def execute(self, query, *args):
        # seed_db upsert: positional args match the INSERT column order.
        abbrev, full_name, field, kind, issn_l, source_id, match_names = args
        self.rows[abbrev] = {
            "abbrev": abbrev,
            "full_name": full_name,
            "field": field,
            "kind": kind,
            "issn_l": issn_l,
            "source_id": source_id,
            "match_names": match_names,
        }

    async def fetchval(self, query, *args):
        if "issn_l = $1" in query:
            return next(
                (r["abbrev"] for r in self.rows.values() if r["issn_l"] == args[0]),
                None,
            )
        if "source_id = $1" in query:
            return next(
                (r["abbrev"] for r in self.rows.values() if r["source_id"] == args[0]),
                None,
            )
        if "ANY(match_names)" in query:
            return next(
                (
                    r["abbrev"]
                    for r in self.rows.values()
                    if args[0] in (r["match_names"] or [])
                ),
                None,
            )
        raise AssertionError(f"Unexpected query: {query}")


class NormalizeTests(unittest.TestCase):
    def test_normalize_name_strips_noise(self):
        self.assertEqual(
            venues.normalize_name("The American Journal of Agricultural Economics"),
            "american agricultural economics",
        )

    def test_lexical_overlap_counts_distinct_query_terms(self):
        self.assertEqual(
            venues.lexical_overlap(
                "water demand price", "Estimating water demand elasticity"
            ),
            2,
        )

    def test_lexical_overlap_ignores_short_tokens(self):
        self.assertEqual(venues.lexical_overlap("a of to", "a of to water"), 0)


class LoadSeedsTests(unittest.TestCase):
    def test_load_seeds_parses_toml(self):
        seeds = venues.load_seeds()
        by_abbrev = {s["abbrev"]: s for s in seeds}
        self.assertIn("AJAE", by_abbrev)
        self.assertEqual(by_abbrev["AJAE"]["issn_l"], "0002-9092")
        # match_names are normalized at load time.
        self.assertIn("amer j agr econ", by_abbrev["AJAE"]["match_names"])
        self.assertIn("ARXIV", by_abbrev)
        self.assertEqual(by_abbrev["ARXIV"]["source_id"], "S4306400194")


class SeedAndMatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_cascade_issn_source_name(self):
        conn = _VenueConnection()
        pool = _Pool(conn)
        seeded = await venues.seed_db(pool)
        self.assertGreater(seeded, 0)
        self.assertEqual(await venues.match(pool, issn_l="0002-9092"), "AJAE")
        self.assertEqual(await venues.match(pool, source_id="S4306400194"), "ARXIV")
        self.assertEqual(await venues.match(pool, name="Amer J Agr Econ"), "AJAE")
        self.assertIsNone(await venues.match(pool, name="Totally Unknown Venue"))


if __name__ == "__main__":
    unittest.main()
