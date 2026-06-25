"""
suppress_garbled_orphans() — drop consecutive garbled-boundary cut pairs that
enclose a span shorter than the threshold (default 30s).

Two consecutive garbled-boundary refinements can bracket a tiny span whose date
label belongs to neither adjacent session. Dropping both cuts merges the orphan
into its neighbour.
"""
from split_homevideo import suppress_garbled_orphans


class TestSuppressGarbledOrphans:
    def test_pair_under_threshold_dropped(self):
        # Converse 1992 case: 6s orphan between two garbled cuts at 1304, 1310
        splits = [0.0, 1304.0, 1310.0, 2000.0]
        garbled = {1304.0, 1310.0}
        result, n = suppress_garbled_orphans(splits, garbled, threshold=30.0)
        assert result == [0.0, 2000.0]
        assert n == 2

    def test_pair_at_threshold_kept(self):
        splits = [0.0, 1000.0, 1030.0, 2000.0]
        garbled = {1000.0, 1030.0}
        result, n = suppress_garbled_orphans(splits, garbled, threshold=30.0)
        assert result == [0.0, 1000.0, 1030.0, 2000.0]
        assert n == 0

    def test_pair_over_threshold_kept(self):
        splits = [0.0, 1000.0, 1035.0, 2000.0]
        garbled = {1000.0, 1035.0}
        result, n = suppress_garbled_orphans(splits, garbled, threshold=30.0)
        assert result == [0.0, 1000.0, 1035.0, 2000.0]
        assert n == 0

    def test_single_garbled_cut_kept(self):
        # One garbled cut with no consecutive garbled partner is not suppressed
        splits = [0.0, 1304.0, 2000.0]
        garbled = {1304.0}
        result, n = suppress_garbled_orphans(splits, garbled, threshold=30.0)
        assert result == [0.0, 1304.0, 2000.0]
        assert n == 0

    def test_non_garbled_cuts_unaffected(self):
        # Short span between non-garbled cuts — suppress_garbled_orphans does nothing
        splits = [0.0, 1000.0, 1005.0, 2000.0]
        garbled: set[float] = set()
        result, n = suppress_garbled_orphans(splits, garbled, threshold=30.0)
        assert result == [0.0, 1000.0, 1005.0, 2000.0]
        assert n == 0

    def test_mixed_only_garbled_pair_dropped(self):
        # One garbled pair (span=6s) and one legitimate boundary — only pair dropped
        splits = [0.0, 500.0, 1304.0, 1310.0, 2000.0]
        garbled = {1304.0, 1310.0}
        result, n = suppress_garbled_orphans(splits, garbled, threshold=30.0)
        assert result == [0.0, 500.0, 2000.0]
        assert n == 2

    def test_chain_three_garbled_boundaries(self):
        # t1, t2, t3 all garbled; span(t1,t2)<30 and span(t2,t3)<30
        # Greedy left-to-right: (t1,t2) dropped first, t3 has no partner → kept
        splits = [0.0, 1000.0, 1005.0, 1010.0, 2000.0]
        garbled = {1000.0, 1005.0, 1010.0}
        result, n = suppress_garbled_orphans(splits, garbled, threshold=30.0)
        assert result == [0.0, 1010.0, 2000.0]
        assert n == 2

    def test_no_splits_unchanged(self):
        splits = [0.0]
        garbled: set[float] = set()
        result, n = suppress_garbled_orphans(splits, garbled, threshold=30.0)
        assert result == [0.0]
        assert n == 0
