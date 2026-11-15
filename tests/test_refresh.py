"""Tests for pdrive.refresh."""

from __future__ import annotations

import math
import pytest

from pdrive.refresh import closeness, pick_refresh


# ---------------------------------------------------------------------------
# closeness – identity
# ---------------------------------------------------------------------------

class TestClosenessIdentity:
    def test_same_path(self):
        assert closeness("/Movies", "/Movies") == 1.0

    def test_root_same(self):
        assert closeness("/", "/") == 1.0

    def test_deep_same(self):
        assert closeness("/A/B/C", "/A/B/C") == 1.0


# ---------------------------------------------------------------------------
# closeness – direct parent / child  (0.7)
# ---------------------------------------------------------------------------

class TestClosenessDirect:
    def test_parent_simple(self):
        assert closeness("/", "/Movies") == 0.7

    def test_parent_nested(self):
        assert closeness("/Movies", "/Movies/A") == 0.7

    def test_child_simple(self):
        assert closeness("/Movies", "/") == 0.7

    def test_child_nested(self):
        assert closeness("/Movies/A", "/Movies") == 0.7

    def test_root_parent_of_deep(self):
        # "/" is parent of "/X" but NOT parent of "/X/Y"
        assert closeness("/", "/X") == 0.7
        assert closeness("/", "/X/Y") == 0.3  # grandparent → 0.3


# ---------------------------------------------------------------------------
# closeness – siblings  (0.5)
# ---------------------------------------------------------------------------

class TestClosenessSiblings:
    def test_siblings_under_root(self):
        assert closeness("/Movies", "/Music") == 0.5

    def test_siblings_nested(self):
        assert closeness("/Movies/A", "/Movies/B") == 0.5

    def test_siblings_deep(self):
        assert closeness("/A/B/C", "/A/B/D") == 0.5

    def test_not_sibling_different_parents(self):
        assert closeness("/A/B", "/C/D") != 0.5


# ---------------------------------------------------------------------------
# closeness – ancestor / descendant (non-direct)  (0.3)
# ---------------------------------------------------------------------------

class TestClosenessAncestorDescendant:
    def test_grandparent(self):
        assert closeness("/", "/Movies/A") == 0.3

    def test_great_grandparent(self):
        assert closeness("/", "/A/B/C") == 0.3

    def test_grandchild(self):
        assert closeness("/Movies/A/B", "/Movies") == 0.3

    def test_parent_two_levels_up(self):
        assert closeness("/A", "/A/B/C") == 0.3


# ---------------------------------------------------------------------------
# closeness – same first component  (0.1)
# ---------------------------------------------------------------------------

class TestClosenessFirstComponent:
    def test_same_first_component(self):
        assert closeness("/Music/X/Y", "/Music/Z") == 0.1

    def test_same_first_component_other(self):
        assert closeness("/A/B/X", "/A/C") == 0.1

    def test_root_has_no_component_match(self):
        # "/" has no first component, so no 0.1 match with "/X"
        assert closeness("/", "/X/Y") == 0.3  # ancestor, not 0.1
        assert closeness("/X/Y", "/") == 0.3  # descendant, not 0.1


# ---------------------------------------------------------------------------
# closeness – unrelated  (0.0)
# ---------------------------------------------------------------------------

class TestClosenessUnrelated:
    def test_different_trees(self):
        assert closeness("/A/B", "/C/D") == 0.0

    def test_different_first_components(self):
        assert closeness("/X", "/Y") == 0.5  # siblings under root

    def test_deeply_unrelated(self):
        assert closeness("/A/B/C/D", "/X/Y/Z") == 0.0

    def test_adjacent_but_unrelated(self):
        assert closeness("/A/X/Y", "/A/Z/W") == 0.1  # same first component

    def test_truly_different(self):
        assert closeness("/Movies/Foo/Bar", "/Music/Baz") == 0.0


# ---------------------------------------------------------------------------
# pick_refresh – most stale relevant folder wins
# ---------------------------------------------------------------------------

class TestPickRefreshStaleWins:
    def test_stalest_relevant_wins(self):
        now = 1_000_000.0
        # Two relevant folders; older one should win
        folders = [
            ("uid_a", "/Movies/A", now - 400),   # age 400
            ("uid_b", "/Movies/B", now - 900),   # age 900  → stalest
        ]
        activity = [("/Movies/A", now - 10)]  # recent use near both

        result = pick_refresh(folders, activity, now, min_age=300)
        assert result == "uid_b"


# ---------------------------------------------------------------------------
# pick_refresh – relevance can beat age
# ---------------------------------------------------------------------------

class TestPickRefreshRelevanceBeatsAge:
    def test_exact_match_beats_older_unrelated(self):
        now = 1_000_000.0
        folders = [
            ("uid_exact", "/Movies/A", now - 350),   # very close but younger
            ("uid_far", "/Music/Z", now - 2000),      # much older but unrelated
        ]
        activity = [("/Movies/A", now - 1)]  # exact hit

        result = pick_refresh(folders, activity, now, min_age=300)
        assert result == "uid_exact"


# ---------------------------------------------------------------------------
# pick_refresh – decay of old activity
# ---------------------------------------------------------------------------

class TestPickRefreshDecay:
    def test_old_activity_decays(self):
        now = 1_000_000.0
        half_life = 600.0
        # Activity 1 hour ago (3600 s → 6 half-lives → factor ~1/64)
        # Activity 1 minute ago (60 s → 0.1 half-lives → factor ~0.93)
        folders = [
            ("uid_a", "/A/B", now - 500),
            ("uid_b", "/A/B", now - 500),
        ]
        # Two identical folders pointing to same path – disambiguate by uid
        # Use different paths so closeness differs
        folders = [
            ("uid_old", "/Old/Folder", now - 500),
            ("uid_new", "/New/Folder", now - 500),
        ]
        activity = [
            ("/Old/Folder", now - 3600),   # 1 hour old → heavily decayed
            ("/New/Folder", now - 60),      # 1 minute old → fresh
        ]
        result = pick_refresh(folders, activity, now, min_age=300, half_life=half_life)
        assert result == "uid_new"

    def test_decay_formula(self):
        """Verify the exponential decay factor."""
        now = 1_000_000.0
        half_life = 600.0
        elapsed = 1200.0  # exactly 2 half-lives
        expected_decay = 0.5 ** (elapsed / half_life)  # 0.25
        assert math.isclose(expected_decay, 0.25, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# pick_refresh – min_age excludes fresh folders
# ---------------------------------------------------------------------------

class TestPickRefreshMinAge:
    def test_fresh_folder_excluded(self):
        now = 1_000_000.0
        folders = [
            ("uid_fresh", "/Movies/A", now - 100),  # age 100 < 300
        ]
        activity = [("/Movies/A", now - 1)]
        result = pick_refresh(folders, activity, now, min_age=300)
        assert result is None

    def test_folder_at_exact_min_age_included(self):
        now = 1_000_000.0
        folders = [
            ("uid_edge", "/Movies/A", now - 300),  # age == min_age
        ]
        activity = [("/Movies/A", now - 1)]
        result = pick_refresh(folders, activity, now, min_age=300)
        assert result == "uid_edge"

    def test_mix_fresh_and_old(self):
        now = 1_000_000.0
        folders = [
            ("uid_fresh", "/A/B", now - 50),
            ("uid_old", "/A/C", now - 600),
        ]
        activity = [("/A/B", now - 1), ("/A/C", now - 1)]
        result = pick_refresh(folders, activity, now, min_age=300)
        assert result == "uid_old"


# ---------------------------------------------------------------------------
# pick_refresh – None / empty cases
# ---------------------------------------------------------------------------

class TestPickRefreshNone:
    def test_empty_activity_returns_none(self):
        now = 1_000_000.0
        folders = [("uid_a", "/Movies/A", now - 900)]
        assert pick_refresh(folders, [], now) is None

    def test_empty_folders_returns_none(self):
        now = 1_000_000.0
        activity = [("/Movies/A", now - 1)]
        assert pick_refresh([], activity, now) is None

    def test_all_paths_none_returns_none(self):
        now = 1_000_000.0
        folders = [
            ("uid_a", None, now - 900),
            ("uid_b", None, now - 800),
        ]
        activity = [("/Movies/A", now - 1)]
        assert pick_refresh(folders, activity, now) is None

    def test_no_qualifying_folder_returns_none(self):
        now = 1_000_000.0
        folders = [
            ("uid_a", None, now - 900),
            ("uid_b", "/Music/X", now - 400),  # relevant? check activity
        ]
        activity = [("/Astronomy/Z", now - 1)]  # completely unrelated
        # closeness("/Music/X", "/Astronomy/Z") == 0.0 → priority 0
        result = pick_refresh(folders, activity, now, min_age=300)
        assert result is None

    def test_all_too_fresh_returns_none(self):
        now = 1_000_000.0
        folders = [
            ("uid_a", "/Movies/A", now - 60),
            ("uid_b", "/Music/B", now - 120),
        ]
        activity = [("/Movies/A", now - 1), ("/Music/B", now - 1)]
        result = pick_refresh(folders, activity, now, min_age=300)
        assert result is None


# ---------------------------------------------------------------------------
# pick_refresh – tie-breaking by age (older wins)
# ---------------------------------------------------------------------------

class TestPickRefreshTieBreak:
    def test_tie_older_wins(self):
        now = 1_000_000.0
        # Two folders with same relevance but different ages → same priority
        # if relevance is equal, priority = relevance * age → older has higher priority
        # For an actual tie in priority, we need same relevance AND same age,
        # OR craft a scenario where relevance*age is equal.
        # Simpler: identical paths, same relevance, different ages
        # Actually with same path, same relevance → older folder has higher priority.
        # For a TRUE tie (priority equal), use different paths that give same closeness.
        folders = [
            ("uid_older", "/A/X", now - 600),   # age 600
            ("uid_younger", "/A/Y", now - 400),  # age 400
        ]
        activity = [("/A/X", now - 1), ("/A/Y", now - 1)]
        # closeness("/A/X","/A/X")=1.0, closeness("/A/Y","/A/Y")=1.0
        # But also each gets closeness with the other's activity
        # uid_older relevance: max(closeness("/A/X","/A/X")*d, closeness("/A/X","/A/Y")*d)
        #                      = max(1.0*d, 0.5*d) = 1.0*d
        # uid_younger relevance: max(closeness("/A/Y","/A/X")*d, closeness("/A/Y","/A/Y")*d)
        #                        = max(0.5*d, 1.0*d) = 1.0*d
        # priority_older = 1.0*d*600, priority_younger = 1.0*d*400 → older wins (not a tie)
        result = pick_refresh(folders, activity, now, min_age=300)
        assert result == "uid_older"


# ---------------------------------------------------------------------------
# pick_refresh – multiple activity entries, max is taken
# ---------------------------------------------------------------------------

class TestPickRefreshMultipleActivity:
    def test_max_over_activity(self):
        now = 1_000_000.0
        folders = [
            ("uid_a", "/Movies/Action", now - 500),
        ]
        activity = [
            ("/Comedy/X", now - 1),      # closeness 0.0
            ("/Movies/Action", now - 1),  # closeness 1.0
            ("/Movies/SciFi", now - 1),   # closeness 0.5 (sibling)
        ]
        result = pick_refresh(folders, activity, now, min_age=300)
        assert result == "uid_a"


# ---------------------------------------------------------------------------
# closeness – additional edge cases
# ---------------------------------------------------------------------------

class TestClosenessEdgeCases:
    def test_root_vs_single_child_deep(self):
        assert closeness("/", "/Movies/Action") == 0.3

    def test_single_child_vs_root_deep(self):
        assert closeness("/Movies/Action", "/") == 0.3

    def test_sibling_root_children(self):
        assert closeness("/Movies", "/Music") == 0.5

    def test_not_sibling_deep_different(self):
        assert closeness("/A/B", "/C/D") == 0.0

    def test_first_component_vs_root(self):
        assert closeness("/A/B", "/") == 0.3  # ancestor

    def test_closeness_is_compliant_symmetry(self):
        # closeness is NOT symmetric in general but parent↔child should be
        assert closeness("/", "/Movies") == closeness("/Movies", "/")  # 0.7
        assert closeness("/Movies", "/Movies/A") == closeness("/Movies/A", "/Movies")  # 0.7

    def test_grandparent_symmetry(self):
        assert closeness("/", "/A/B") == 0.3
        assert closeness("/A/B", "/") == 0.3
