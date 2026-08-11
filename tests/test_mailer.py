"""Email notifications.

Two properties matter more than the message text: a player only ever gets what
they opted into, and a broken mail server never breaks the ladder.
"""

import unittest

from ladder import divisions as div
from ladder.mailer import KIND_LABELS, Mailer, unsubscribe_token, verify_unsubscribe
from ladder.service import LadderService
from ladder.storage import Database
from tests.helpers import days_ago, make_config

MS = div.MENS_SINGLES
MD = div.MENS_DOUBLES


def mail_config(**overrides):
    base = dict(smtp_host="smtp.example.edu", smtp_from="ladder@example.edu",
                base_url="https://ladder.example.edu")
    base.update(overrides)
    return make_config(**base)


class MailerTestCase(unittest.TestCase):
    def setUp(self, **config_overrides):
        self.config = mail_config(**config_overrides)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.outbox = []
        self.mailer = Mailer(self.config, self.db,
                             sender=lambda message: self.outbox.append(message))
        self.service = LadderService(self.db, self.config,
                                     notifier=self.mailer.send)
        self.al = self.db.add_player("Al", "al@example.edu", category=div.MENS)
        self.bo = self.db.add_player("Bo", "bo@example.edu", category=div.MENS)

    def submit(self, **kwargs):
        base = dict(division=MS, side_a=[self.al.id], side_b=[self.bo.id],
                    score_text="6-4 6-4", played_on=days_ago(1),
                    submitted_by=self.al.id)
        base.update(kwargs)
        result = self.service.submit_result(**base)
        self.mailer.flush()
        return result

    def recipients(self):
        return sorted(m["To"] for m in self.outbox)


class TestConfirmationEmails(MailerTestCase):
    def test_the_opponent_is_told_a_result_needs_confirming(self):
        self.submit()
        self.assertEqual(self.recipients(), ["bo@example.edu"])
        self.assertIn("Confirm", self.outbox[0]["Subject"])

    def test_the_submitter_is_not_emailed_their_own_submission(self):
        self.submit()
        self.assertNotIn("al@example.edu", self.recipients())

    def test_nothing_is_sent_for_an_auto_confirmed_result(self):
        self.submit(auto_confirm=True)
        self.assertEqual(self.outbox, [])

    def test_the_submitter_hears_when_it_is_confirmed(self):
        result = self.submit()
        self.outbox.clear()
        self.service.confirm(result.match_id, self.bo.id)
        self.mailer.flush()
        self.assertEqual(self.recipients(), ["al@example.edu"])
        self.assertIn("confirmed", self.outbox[0]["Subject"].lower())

    def test_the_submitter_hears_when_it_is_disputed(self):
        result = self.submit()
        self.outbox.clear()
        self.service.reject(result.match_id, self.bo.id)
        self.mailer.flush()
        self.assertIn("disputed", self.outbox[0]["Subject"].lower())

    def test_doubles_notifies_both_opponents_but_not_your_partner(self):
        cy = self.db.add_player("Cy", "cy@example.edu", category=div.MENS)
        dan = self.db.add_player("Dan", "dan@example.edu", category=div.MENS)
        self.submit(division=MD, side_a=[self.al.id, self.bo.id],
                    side_b=[cy.id, dan.id])
        self.assertEqual(self.recipients(), ["cy@example.edu", "dan@example.edu"])

    def test_the_body_names_the_players_and_the_score(self):
        self.submit()
        body = self.outbox[0].get_content()
        self.assertIn("Al", body)
        self.assertIn("6-4 6-4", body)
        self.assertIn("https://ladder.example.edu/pending", body)


class TestOptIn(MailerTestCase):
    def test_a_player_who_opted_out_gets_nothing(self):
        self.db.set_notification(self.bo.id, "confirm", False)
        self.submit()
        self.assertEqual(self.outbox, [])

    def test_toggles_are_independent(self):
        """Turning off confirmations must not silence result notices."""
        self.db.set_notification(self.al.id, "confirm", False)
        result = self.submit()
        self.outbox.clear()
        self.service.confirm(result.match_id, self.bo.id)
        self.mailer.flush()
        self.assertEqual(self.recipients(), ["al@example.edu"])

    def test_a_player_with_no_email_is_skipped(self):
        self.db.set_email(self.bo.id, "")
        self.submit()
        self.assertEqual(self.outbox, [])

    def test_weekly_summaries_only_reach_those_who_asked(self):
        self.db.set_notification(self.al.id, "weekly", True)
        sent = self.mailer.send_weekly_summary(["Standings..."], "This week")
        self.mailer.flush()
        self.assertEqual(sent, 1)
        self.assertEqual(self.recipients(), ["al@example.edu"])

    def test_season_notices_respect_their_own_toggle(self):
        self.db.set_notification(self.bo.id, "season", False)
        self.service.start_season("Season 2")
        self.mailer.flush()
        self.assertEqual(self.recipients(), ["al@example.edu"])

    def test_every_message_carries_an_unsubscribe_link(self):
        self.submit()
        message = self.outbox[0]
        self.assertIn("/unsubscribe?", message.get_content())
        self.assertIn("List-Unsubscribe", message)


class TestTestEmail(MailerTestCase):
    """The setup aid: one email, sent now, with the real error if it fails.

    Configuring SMTP otherwise means changing a file, restarting, waiting, and
    guessing why nothing arrived. This turns that into one click and a
    sentence.
    """

    def test_a_successful_send_says_so(self):
        outcome = self.mailer.send_test("captain@example.edu")
        self.assertTrue(outcome.startswith("Sent"))
        self.assertEqual(self.recipients(), ["captain@example.edu"])

    def test_the_smtp_error_is_reported_rather_than_swallowed(self):
        """Everywhere else failures are swallowed so a dead mail server can't
        break a result. Here the error is the entire point."""
        def refuse(message):
            raise OSError("[Errno 111] Connection refused")

        self.mailer._sender = refuse
        outcome = self.mailer.send_test("captain@example.edu")
        self.assertTrue(outcome.startswith("Failed"))
        self.assertIn("Connection refused", outcome)

    def test_an_unconfigured_club_is_told_what_is_missing(self):
        from ladder.mailer import Mailer
        from tests.helpers import make_config

        bare = Mailer(make_config(), self.db, sender=lambda m: None)
        self.assertIn("smtp_host", bare.send_test("captain@example.edu"))

    def test_a_bad_address_is_caught_before_sending(self):
        self.assertIn("email address", self.mailer.send_test("not-an-address"))
        self.assertEqual(self.outbox, [])

    def test_it_ignores_notification_preferences(self):
        """A test email is for the admin checking the plumbing, not a
        notification -- opting out of everything must not silence it."""
        for kind in ("confirm", "result", "weekly", "season"):
            self.db.set_notification(self.al.id, kind, False)
        self.assertTrue(self.mailer.send_test(self.al.email).startswith("Sent"))


class TestUnsubscribeTokens(unittest.TestCase):
    def test_a_token_verifies_for_its_own_player_and_kind(self):
        token = unsubscribe_token("s3cret", 7, "confirm")
        self.assertTrue(verify_unsubscribe("s3cret", 7, "confirm", token))

    def test_a_token_does_not_work_for_another_player(self):
        token = unsubscribe_token("s3cret", 7, "confirm")
        self.assertFalse(verify_unsubscribe("s3cret", 8, "confirm", token))

    def test_a_token_does_not_work_for_another_notification_type(self):
        token = unsubscribe_token("s3cret", 7, "confirm")
        self.assertFalse(verify_unsubscribe("s3cret", 7, "weekly", token))

    def test_a_forged_token_is_rejected(self):
        self.assertFalse(verify_unsubscribe("s3cret", 7, "confirm", "deadbeef"))
        self.assertFalse(verify_unsubscribe("s3cret", 7, "confirm", ""))

    def test_every_notification_kind_is_covered(self):
        self.assertEqual(set(KIND_LABELS),
                         {"confirm", "result", "weekly", "season"})


class TestFailuresAreContained(unittest.TestCase):
    """A dead mail server is an inconvenience, never a broken ladder."""

    def setUp(self):
        self.config = mail_config()
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)

        def explode(message):
            raise OSError("connection refused")

        self.mailer = Mailer(self.config, self.db, sender=explode)
        self.service = LadderService(self.db, self.config,
                                     notifier=self.mailer.send)
        self.al = self.db.add_player("Al", "al@example.edu", category=div.MENS)
        self.bo = self.db.add_player("Bo", "bo@example.edu", category=div.MENS)

    def test_a_broken_smtp_server_does_not_break_submission(self):
        result = self.service.submit_result(
            division=MS, side_a=[self.al.id], side_b=[self.bo.id],
            score_text="6-4 6-4", played_on=days_ago(1), submitted_by=self.al.id)
        self.mailer.flush()
        self.assertEqual(self.db.get_match(result.match_id).status, "pending")
        self.assertEqual(self.mailer.failures, 1)

    def test_a_broken_smtp_server_does_not_break_confirmation(self):
        result = self.service.submit_result(
            division=MS, side_a=[self.al.id], side_b=[self.bo.id],
            score_text="6-4 6-4", played_on=days_ago(1), submitted_by=self.al.id)
        self.service.confirm(result.match_id, self.bo.id)
        self.mailer.flush()
        self.assertEqual(self.db.get_match(result.match_id).status, "confirmed")


class TestDisabledByDefault(unittest.TestCase):
    def test_nothing_is_sent_when_smtp_is_not_configured(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        config = make_config()                    # no smtp_host
        outbox = []
        mailer = Mailer(config, db, sender=outbox.append)
        service = LadderService(db, config, notifier=mailer.send)
        al = db.add_player("Al", "al@example.edu", category=div.MENS)
        bo = db.add_player("Bo", "bo@example.edu", category=div.MENS)
        service.submit_result(division=MS, side_a=[al.id], side_b=[bo.id],
                              score_text="6-4 6-4", played_on=days_ago(1),
                              submitted_by=al.id)
        mailer.flush()
        self.assertFalse(config.email_enabled)
        self.assertEqual(outbox, [])


if __name__ == "__main__":
    unittest.main()
