"""The division table and lineup validation, at unit level."""

import unittest
from dataclasses import dataclass

from ladder import divisions as div


@dataclass
class FakePlayer:
    id: int
    name: str
    category: str


def man(i: int) -> FakePlayer:
    return FakePlayer(i, f"M{i}", div.MENS)


def woman(i: int) -> FakePlayer:
    return FakePlayer(i, f"W{i}", div.WOMENS)


def unknown(i: int) -> FakePlayer:
    return FakePlayer(i, f"U{i}", div.UNSPECIFIED)


class TestTable(unittest.TestCase):
    def test_all_five_divisions_exist(self):
        self.assertEqual(len(div.DIVISION_ORDER), 5)
        self.assertEqual(set(div.DIVISION_ORDER), set(div.DIVISIONS))

    def test_team_sizes(self):
        self.assertEqual(div.get(div.MENS_SINGLES).team_size, 1)
        self.assertEqual(div.get(div.MIXED_DOUBLES).team_size, 2)
        self.assertTrue(div.get(div.WOMENS_DOUBLES).is_doubles)
        self.assertFalse(div.get(div.WOMENS_SINGLES).is_doubles)

    def test_only_mixed_is_mixed(self):
        mixed = [d.key for d in div.all_divisions() if d.is_mixed]
        self.assertEqual(mixed, [div.MIXED_DOUBLES])

    def test_unknown_divisions_raise(self):
        with self.assertRaises(div.DivisionError):
            div.get("beach_volleyball")
        self.assertFalse(div.is_division("beach_volleyball"))


class TestSeeding(unittest.TestCase):
    def test_doubles_seeds_from_the_matching_singles_ladder(self):
        self.assertEqual(div.seed_division_for(div.MENS_DOUBLES, div.MENS),
                         div.MENS_SINGLES)
        self.assertEqual(div.seed_division_for(div.WOMENS_DOUBLES, div.WOMENS),
                         div.WOMENS_SINGLES)

    def test_mixed_seeds_from_the_players_own_category(self):
        self.assertEqual(div.seed_division_for(div.MIXED_DOUBLES, div.MENS),
                         div.MENS_SINGLES)
        self.assertEqual(div.seed_division_for(div.MIXED_DOUBLES, div.WOMENS),
                         div.WOMENS_SINGLES)

    def test_singles_has_nothing_to_seed_from(self):
        self.assertIsNone(div.seed_division_for(div.MENS_SINGLES, div.MENS))

    def test_an_uncategorised_player_has_no_mixed_seed(self):
        self.assertIsNone(div.seed_division_for(div.MIXED_DOUBLES, div.UNSPECIFIED))


class TestLineupValidation(unittest.TestCase):
    def ok(self, division, side_a, side_b, **kwargs):
        div.validate_lineup(division, side_a, side_b, **kwargs)

    def fails(self, division, side_a, side_b, **kwargs):
        with self.assertRaises(div.DivisionError) as ctx:
            div.validate_lineup(division, side_a, side_b, **kwargs)
        return str(ctx.exception)

    def test_valid_lineups_pass(self):
        self.ok(div.MENS_SINGLES, [man(1)], [man(2)])
        self.ok(div.WOMENS_DOUBLES, [woman(1), woman(2)], [woman(3), woman(4)])
        self.ok(div.MIXED_DOUBLES, [man(1), woman(2)], [woman(3), man(4)])

    def test_wrong_team_size_is_caught(self):
        message = self.fails(div.MENS_SINGLES, [man(1), man(2)], [man(3), man(4)])
        self.assertIn("one player", message)
        message = self.fails(div.MENS_DOUBLES, [man(1)], [man(2)])
        self.assertIn("two players", message)

    def test_a_player_appearing_twice_is_caught(self):
        message = self.fails(div.MENS_DOUBLES, [man(1), man(2)], [man(1), man(3)])
        self.assertIn("twice", message)

    def test_wrong_category_is_caught_and_names_the_player(self):
        message = self.fails(div.MENS_SINGLES, [woman(1)], [man(2)])
        self.assertIn("W1", message)
        self.assertIn("Men's", message)

    def test_uncategorised_players_cannot_enter_a_gendered_division(self):
        message = self.fails(div.MENS_SINGLES, [unknown(1)], [man(2)])
        self.assertIn("U1", message)

    def test_mixed_requires_one_of_each_on_every_side(self):
        message = self.fails(div.MIXED_DOUBLES, [man(1), man(2)],
                             [man(3), woman(4)])
        self.assertIn("one player from each", message)
        self.assertIn("first side", message)

    def test_mixed_catches_a_bad_second_side_too(self):
        message = self.fails(div.MIXED_DOUBLES, [man(1), woman(2)],
                             [woman(3), woman(4)])
        self.assertIn("second side", message)

    def test_override_skips_category_rules_but_not_structural_ones(self):
        self.ok(div.MENS_SINGLES, [woman(1)], [man(2)], override=True)
        self.ok(div.MIXED_DOUBLES, [man(1), man(2)], [man(3), man(4)],
                override=True)
        # Team size and duplicate players are data errors, not policy.
        self.fails(div.MENS_SINGLES, [man(1), man(2)], [man(3), man(4)],
                   override=True)
        self.fails(div.MENS_DOUBLES, [man(1), man(2)], [man(1), man(3)],
                   override=True)


class TestEligibility(unittest.TestCase):
    def test_a_man_can_enter_mens_and_mixed(self):
        keys = [d.key for d in div.divisions_for_category(div.MENS)]
        self.assertEqual(keys, [div.MENS_SINGLES, div.MENS_DOUBLES,
                                div.MIXED_DOUBLES])

    def test_a_woman_can_enter_womens_and_mixed(self):
        keys = [d.key for d in div.divisions_for_category(div.WOMENS)]
        self.assertEqual(keys, [div.WOMENS_SINGLES, div.WOMENS_DOUBLES,
                                div.MIXED_DOUBLES])

    def test_an_uncategorised_player_can_enter_nothing_until_set(self):
        self.assertEqual(div.divisions_for_category(div.UNSPECIFIED), [])


if __name__ == "__main__":
    unittest.main()
