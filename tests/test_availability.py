"""Availability: interval maths, and turning it into times two people can play.

The feature only works if people fill it in, so the model is a weekly pattern
plus one-tap exceptions. These tests pin the arithmetic underneath that.
"""

import unittest
from datetime import date, datetime

from ladder.availability import (
    Availability, WEEKDAY_SHORT, clock, covers, describe_week, intersect,
    intervals_from_slots, long_enough, merge, mutual_slots, parse_clock,
    slot_grid, subtract,
)

MON, TUE, WED, THU = 0, 1, 2, 3


def at(hour, minute=0):
    return hour * 60 + minute


class TestIntervalMaths(unittest.TestCase):
    def test_merge_coalesces_touching_and_overlapping(self):
        self.assertEqual(merge([(540, 600), (600, 660)]), [(540, 660)])
        self.assertEqual(merge([(540, 660), (600, 700)]), [(540, 700)])
        self.assertEqual(merge([(800, 900), (540, 600)]), [(540, 600), (800, 900)])

    def test_merge_drops_empty_intervals(self):
        self.assertEqual(merge([(600, 600), (540, 600)]), [(540, 600)])

    def test_subtract_punches_a_hole(self):
        self.assertEqual(subtract([(540, 720)], [(600, 630)]),
                         [(540, 600), (630, 720)])

    def test_subtract_can_remove_everything(self):
        self.assertEqual(subtract([(540, 720)], [(0, 1440)]), [])

    def test_subtract_ignores_non_overlapping_holes(self):
        self.assertEqual(subtract([(540, 720)], [(800, 900)]), [(540, 720)])

    def test_subtract_trims_at_the_edges(self):
        self.assertEqual(subtract([(540, 720)], [(500, 560)]), [(560, 720)])
        self.assertEqual(subtract([(540, 720)], [(700, 800)]), [(540, 700)])

    def test_intersect_finds_the_shared_window(self):
        self.assertEqual(intersect([(540, 720)], [(660, 900)]), [(660, 720)])

    def test_intersect_of_disjoint_is_empty(self):
        self.assertEqual(intersect([(540, 600)], [(700, 800)]), [])

    def test_long_enough_filters_short_gaps(self):
        self.assertEqual(long_enough([(540, 600), (660, 900)], 90), [(660, 900)])


class TestClock(unittest.TestCase):
    def test_formatting_is_twelve_hour(self):
        self.assertEqual(clock(at(17)), "5:00pm")
        self.assertEqual(clock(at(9, 30)), "9:30am")
        self.assertEqual(clock(at(12)), "12:00pm")
        self.assertEqual(clock(0), "12:00am")

    def test_parsing_accepts_what_people_type(self):
        for text in ("5pm", "17:00", "5:00pm"):
            self.assertEqual(parse_clock(text), at(17), text)
        self.assertEqual(parse_clock("9:30am"), at(9, 30))
        self.assertEqual(parse_clock("12am"), 0)

    def test_parsing_rejects_nonsense(self):
        for bad in ("", "lunchtime", "25:00", "5:99"):
            with self.assertRaises(ValueError, msg=bad):
                parse_clock(bad)


class TestResolvingADate(unittest.TestCase):
    def setUp(self):
        # Free Tuesdays 3-6pm.
        self.a = Availability(weekly={TUE: [(at(15), at(18))]},
                              blocked={}, extra={})

    def test_the_weekly_pattern_applies_to_every_matching_date(self):
        self.assertEqual(self.a.on(date(2026, 8, 11)), [(at(15), at(18))])
        self.assertEqual(self.a.on(date(2026, 8, 18)), [(at(15), at(18))])

    def test_other_weekdays_are_empty(self):
        self.assertEqual(self.a.on(date(2026, 8, 12)), [])

    def test_blocking_one_date_leaves_the_rest_alone(self):
        self.a.blocked["2026-08-11"] = [(0, 1440)]
        self.assertEqual(self.a.on(date(2026, 8, 11)), [])
        self.assertEqual(self.a.on(date(2026, 8, 18)), [(at(15), at(18))])

    def test_blocking_part_of_a_day(self):
        self.a.blocked["2026-08-11"] = [(at(16), at(17))]
        self.assertEqual(self.a.on(date(2026, 8, 11)),
                         [(at(15), at(16)), (at(17), at(18))])

    def test_a_one_off_extra_slot(self):
        self.a.extra["2026-08-15"] = [(at(10), at(13))]     # a Saturday
        self.assertEqual(self.a.on(date(2026, 8, 15)), [(at(10), at(13))])

    def test_a_block_beats_an_extra_on_the_same_day(self):
        """Blocks apply first so 'actually I can't' always wins."""
        self.a.extra["2026-08-15"] = [(at(10), at(13))]
        self.a.blocked["2026-08-15"] = [(at(10), at(13))]
        self.assertEqual(self.a.on(date(2026, 8, 15)), [])

    def test_has_any(self):
        self.assertTrue(self.a.has_any())
        self.assertFalse(Availability.empty().has_any())


class TestMutualSlots(unittest.TestCase):
    def setUp(self):
        # Tiago: Tue 3-6pm, Thu 4-10pm.  Sam: Tue 4-8pm, Thu 9am-5pm.
        self.tiago = Availability(
            weekly={TUE: [(at(15), at(18))], THU: [(at(16), at(22))]},
            blocked={}, extra={})
        self.sam = Availability(
            weekly={TUE: [(at(16), at(20))], THU: [(at(9), at(17))]},
            blocked={}, extra={})
        self.monday = date(2026, 8, 10)
        self.dawn = datetime(2026, 8, 10, 0, 0)

    def find(self, minutes, **kwargs):
        return mutual_slots(self.tiago, self.sam, minutes=minutes,
                            start_day=self.monday, days=7, earliest=self.dawn,
                            **kwargs)

    def test_it_finds_the_overlap(self):
        slots = self.find(60)
        self.assertEqual(slots[0].on, date(2026, 8, 11))
        self.assertEqual((slots[0].start, slots[0].end), (at(16), at(18)))

    def test_a_longer_format_rules_out_short_gaps(self):
        """Thursday's overlap is only 4-5pm, so best-of-three doesn't fit."""
        one_set = [s.label() for s in self.find(60)]
        long_match = [s.label() for s in self.find(120)]
        self.assertEqual(len(one_set), 2)
        self.assertEqual(len(long_match), 1)
        self.assertNotIn("Thu", long_match[0])

    def test_no_overlap_returns_nothing(self):
        hermit = Availability(weekly={MON: [(at(6), at(7))]}, blocked={}, extra={})
        self.assertEqual(
            mutual_slots(self.tiago, hermit, minutes=60,
                         start_day=self.monday, days=7, earliest=self.dawn), [])

    def test_a_player_with_no_availability_has_no_slots(self):
        self.assertEqual(
            mutual_slots(self.tiago, Availability.empty(), minutes=60,
                         start_day=self.monday, days=7, earliest=self.dawn), [])

    def test_results_are_soonest_first(self):
        slots = self.find(60)
        self.assertEqual(slots, sorted(slots, key=lambda s: s.starts_at))

    def test_a_deadline_cuts_the_search_short(self):
        self.assertEqual(self.find(60, not_after=date(2026, 8, 10)), [])
        self.assertTrue(self.find(60, not_after=date(2026, 8, 11)))

    def test_a_blocked_week_removes_that_slot_only(self):
        self.tiago.blocked["2026-08-11"] = [(0, 1440)]
        labels = [s.label() for s in mutual_slots(
            self.tiago, self.sam, minutes=60, start_day=self.monday, days=14,
            earliest=self.dawn)]
        self.assertNotIn("Tue 11 Aug 4:00pm-6:00pm", labels)
        self.assertIn("Tue 18 Aug 4:00pm-6:00pm", labels)

    def test_times_already_past_today_are_not_suggested(self):
        late = datetime(2026, 8, 11, 17, 30)     # mid-way through the Tue slot
        slots = mutual_slots(self.tiago, self.sam, minutes=60,
                             start_day=date(2026, 8, 11), days=1, earliest=late)
        self.assertEqual(slots, [])              # only 30 minutes left

    def test_a_partly_used_window_is_trimmed_not_dropped(self):
        early = datetime(2026, 8, 11, 16, 30)
        slots = mutual_slots(self.tiago, self.sam, minutes=60,
                             start_day=date(2026, 8, 11), days=1, earliest=early)
        self.assertEqual((slots[0].start, slots[0].end), (at(16, 30), at(18)))


class TestGridHelpers(unittest.TestCase):
    def test_the_grid_covers_the_configured_day(self):
        blocks = slot_grid(at(8), at(22), 60)
        self.assertEqual(len(blocks), 14)
        self.assertEqual(blocks[0], (at(8), at(9)))
        self.assertEqual(blocks[-1], (at(21), at(22)))

    def test_ticked_boxes_become_merged_intervals(self):
        weekly = intervals_from_slots(
            [f"{TUE}-{at(15)}-{at(16)}", f"{TUE}-{at(16)}-{at(17)}",
             f"{THU}-{at(9)}-{at(10)}"])
        self.assertEqual(weekly[TUE], [(at(15), at(17))])   # contiguous merged
        self.assertEqual(weekly[THU], [(at(9), at(10))])

    def test_malformed_boxes_are_ignored(self):
        self.assertEqual(intervals_from_slots(["nonsense", "9-1-2", "1-x-y"]), {})

    def test_covers_reports_whether_a_block_is_inside(self):
        self.assertTrue(covers([(at(15), at(18))], (at(16), at(17))))
        self.assertFalse(covers([(at(15), at(18))], (at(18), at(19))))

    def test_describing_a_week_reads_naturally(self):
        text = describe_week({TUE: [(at(15), at(18))], THU: [(at(16), at(22))]})
        self.assertEqual(text, "Tue 3:00pm-6:00pm, Thu 4:00pm-10:00pm")

    def test_an_empty_week_says_so(self):
        self.assertEqual(describe_week({}), "nothing set yet")

    def test_weekday_labels_start_on_monday(self):
        self.assertEqual(WEEKDAY_SHORT[0], "Mon")


if __name__ == "__main__":
    unittest.main()
