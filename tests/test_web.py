"""Web layer: routes, workflows, auth and escaping.

These run a real server on a loopback port and talk to it over HTTP, so the
cookie/session/CSRF plumbing is exercised the way a browser would.
"""

import re
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from ladder import divisions as div
from ladder.mailer import Mailer
from ladder.storage import Database
from ladder.web import App, make_handler
from tests.helpers import days_ago, make_config

MS, MD, XD = div.MENS_SINGLES, div.MENS_DOUBLES, div.MIXED_DOUBLES


class WebTestCase(unittest.TestCase):
    """One server + one browser-like session per test."""

    config_overrides: dict = {}

    def setUp(self):
        self.config = make_config(**self.config_overrides)
        self.db = Database(":memory:")
        self.outbox = []
        mailer = Mailer(self.config, self.db, sender=self.outbox.append)
        self.app = App(self.db, self.config, mailer=mailer)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.db.close()

    # ------------------------------------------------------------ HTTP verbs
    def get(self, path):
        try:
            response = self.opener.open(self.base + path)
            return response.status, response.read().decode(), response.url
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), exc.url

    def post(self, path, data):
        payload = urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(self.base + path, data=payload)
        try:
            response = self.opener.open(request)
            return response.status, response.read().decode(), response.url
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), exc.url

    # -------------------------------------------------------------- helpers
    def csrf(self, path="/login"):
        """The CSRF token for this session. It belongs to the session rather
        than the page, so any page carrying a form will do."""
        for candidate in (path, "/login"):
            _, body, _ = self.get(candidate)
            match = re.search(r'name="csrf" value="([^"]+)"', body)
            if match:
                return match.group(1)
        self.fail(f"no CSRF token available (looked at {path} and /login)")

    def flashes(self, body, kind):
        return re.findall(r'class="flash %s">(.*?)</div>' % kind, body, re.S)

    def login_as(self, player, pin):
        status, body, _ = self.post("/login", {
            "csrf": self.csrf(), "player_id": player.id, "pin": pin})
        self.assertEqual(status, 200)
        return body

    def login_admin(self):
        return self.post("/login", {"csrf": self.csrf(), "as_admin": "1",
                                    "admin_password": "secret"})

    def add(self, name, category=div.MENS, email="", pin="1234"):
        """Add a player with a PIN already set, for tests that just need to
        sign in. Players normally arrive with no PIN and choose one on first
        sign-in -- that flow is covered in TestClaimingAnAccount."""
        player = self.app.service.add_player(name, email, "", category)
        if pin:
            self.db.set_pin(player.id, pin)
            player = self.db.get_player(player.id)
        return player, pin

    def roster(self):
        self.al, self.al_pin = self.add("Al")
        self.bo, self.bo_pin = self.add("Bo")
        self.cy, self.cy_pin = self.add("Cy")
        self.dan, self.dan_pin = self.add("Dan")
        self.eve, self.eve_pin = self.add("Eve", div.WOMENS)
        self.fay, self.fay_pin = self.add("Fay", div.WOMENS)


class TestPublicPages(WebTestCase):
    def test_every_public_page_renders(self):
        self.roster()
        paths = ["/", "/submit", "/pending", "/matches", "/about", "/login",
                 "/export/matches.csv"]
        paths += [f"/?division={d}" for d in div.DIVISION_ORDER]
        paths += [f"/api/ladder.json?division={d}" for d in div.DIVISION_ORDER]
        paths += [f"/export/ladder.csv?division={d}" for d in div.DIVISION_ORDER]
        for path in paths:
            status, body, _ = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body.strip(), path)

    def test_unknown_paths_are_404(self):
        for path in ("/nope", "/player/9999"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_every_division_appears_in_the_navigation(self):
        _, body, _ = self.get("/")
        for key in div.DIVISION_ORDER:
            self.assertIn(f"/?division={key}", body)

    def test_ladder_json_is_wellformed(self):
        import json
        self.roster()
        self.app.service.submit_result(
            division=MS, side_a=[self.al.id], side_b=[self.bo.id],
            score_text="6-4 6-4", played_on=days_ago(3), auto_confirm=True)
        payload = json.loads(self.get(f"/api/ladder.json?division={MS}")[1])
        self.assertEqual(payload["rating_system"], "glicko2")
        self.assertEqual(payload["division"], MS)
        self.assertEqual(len(payload["standings"]), 2)
        self.assertEqual(payload["standings"][0]["rank"], 1)

    def test_player_names_are_escaped(self):
        nasty, _ = self.add("<script>alert(1)</script>")
        self.app.service.submit_result(
            division=MS, side_a=[nasty.id],
            side_b=[self.add("Safe")[0].id], score_text="6-4 6-4",
            played_on=days_ago(2), auto_confirm=True)
        for path in ("/", "/matches", f"/player/{nasty.id}"):
            _, body, _ = self.get(path)
            self.assertNotIn("<script>alert(1)</script>", body, path)
            self.assertIn("&lt;script&gt;", body, path)

    def test_admin_is_closed_to_anonymous_visitors(self):
        self.assertEqual(self.get("/admin")[0], 403)


class TestSubmitAndConfirmFlow(WebTestCase):
    def setUp(self):
        super().setUp()
        self.roster()

    def test_singles_workflow_from_submission_to_ladder(self):
        self.login_as(self.al, self.al_pin)
        _, body, _ = self.post("/submit", {
            "csrf": self.csrf("/submit"), "division": MS,
            "a1": self.al.id, "b1": self.bo.id,
            "score": "6-4 3-6 10-8", "played_on": days_ago(1), "note": "challenge"})
        self.assertTrue(self.flashes(body, "ok"))
        match = self.db.list_matches()[0]
        self.assertEqual(match.status, "pending")

        # Al submitted, so Al cannot also confirm.
        _, body, _ = self.post("/pending", {
            "csrf": self.csrf(), "match_id": match.id, "action": "confirm"})
        self.assertTrue(self.flashes(body, "err"))
        self.assertEqual(self.db.get_match(match.id).status, "pending")

        self.get("/logout")
        self.login_as(self.bo, self.bo_pin)
        _, body, _ = self.post("/pending", {
            "csrf": self.csrf(), "match_id": match.id, "action": "confirm"})
        self.assertTrue(self.flashes(body, "ok"))
        self.assertEqual(self.db.get_match(match.id).status, "confirmed")
        self.assertEqual(self.app.service.engine.ladder(MS).entry(self.al.id).played, 1)

    def test_doubles_submission_takes_four_players(self):
        self.login_as(self.al, self.al_pin)
        self.post("/submit", {
            "csrf": self.csrf("/submit"), "division": MD,
            "a1": self.al.id, "a2": self.bo.id,
            "b1": self.cy.id, "b2": self.dan.id,
            "score": "6-4 6-4", "played_on": days_ago(1)})
        match = self.db.list_matches()[0]
        self.assertTrue(match.is_doubles)
        self.assertEqual(sorted(match.players),
                         sorted([self.al.id, self.bo.id, self.cy.id, self.dan.id]))

    def test_a_doubles_partner_cannot_confirm_but_an_opponent_can(self):
        self.login_as(self.al, self.al_pin)
        self.post("/submit", {
            "csrf": self.csrf("/submit"), "division": MD,
            "a1": self.al.id, "a2": self.bo.id,
            "b1": self.cy.id, "b2": self.dan.id,
            "score": "6-4 6-4", "played_on": days_ago(1)})
        match = self.db.list_matches()[0]

        self.get("/logout")
        self.login_as(self.bo, self.bo_pin)          # the partner
        _, body, _ = self.post("/pending", {
            "csrf": self.csrf(), "match_id": match.id, "action": "confirm"})
        self.assertTrue(self.flashes(body, "err"))
        self.assertEqual(self.db.get_match(match.id).status, "pending")

        self.get("/logout")
        self.login_as(self.cy, self.cy_pin)          # an opponent
        self.post("/pending", {"csrf": self.csrf(), "match_id": match.id,
                               "action": "confirm"})
        self.assertEqual(self.db.get_match(match.id).status, "confirmed")

    def test_a_wrong_category_lineup_is_refused(self):
        self.login_as(self.al, self.al_pin)
        _, body, _ = self.post("/submit", {
            "csrf": self.csrf("/submit"), "division": MS,
            "a1": self.eve.id, "b1": self.al.id,
            "score": "6-4 6-4", "played_on": days_ago(1)})
        self.assertTrue(self.flashes(body, "err"))
        self.assertEqual(self.db.list_matches(), [])

    def test_a_valid_mixed_lineup_is_accepted(self):
        self.login_as(self.al, self.al_pin)
        self.post("/submit", {
            "csrf": self.csrf("/submit"), "division": XD,
            "a1": self.al.id, "a2": self.eve.id,
            "b1": self.bo.id, "b2": self.fay.id,
            "score": "6-4 6-4", "played_on": days_ago(1)})
        self.assertEqual(len(self.db.list_matches()), 1)

    def test_an_unreadable_score_is_refused_with_guidance(self):
        self.login_as(self.al, self.al_pin)
        _, body, _ = self.post("/submit", {
            "csrf": self.csrf("/submit"), "division": MS,
            "a1": self.al.id, "b1": self.bo.id,
            "score": "I won easily", "played_on": days_ago(1)})
        self.assertTrue(self.flashes(body, "err"))
        self.assertEqual(self.db.list_matches(), [])

    def test_disputing_keeps_a_result_off_the_ladder(self):
        self.login_as(self.al, self.al_pin)
        self.post("/submit", {
            "csrf": self.csrf("/submit"), "division": MS,
            "a1": self.al.id, "b1": self.bo.id,
            "score": "6-0 6-0", "played_on": days_ago(1)})
        match = self.db.list_matches()[0]
        self.get("/logout")
        self.login_as(self.bo, self.bo_pin)
        self.post("/pending", {"csrf": self.csrf(), "match_id": match.id,
                               "action": "reject"})
        self.assertEqual(self.db.get_match(match.id).status, "rejected")
        self.assertEqual(self.app.service.engine.ladder(MS).entries, [])


class TestAuth(WebTestCase):
    def setUp(self):
        super().setUp()
        self.roster()

    def test_a_wrong_pin_is_rejected(self):
        wrong = "0000" if self.al_pin != "0000" else "1111"
        _, body, _ = self.post("/login", {
            "csrf": self.csrf(), "player_id": self.al.id, "pin": wrong})
        self.assertTrue(self.flashes(body, "err"))

    def test_a_wrong_admin_password_is_rejected(self):
        _, body, _ = self.post("/login", {
            "csrf": self.csrf(), "as_admin": "1", "admin_password": "wrong"})
        self.assertTrue(self.flashes(body, "err"))
        self.assertEqual(self.get("/admin")[0], 403)

    def test_admin_password_opens_the_admin_area(self):
        self.login_admin()
        self.assertEqual(self.get("/admin")[0], 200)

    def test_signing_out_drops_admin_rights(self):
        self.login_admin()
        self.get("/logout")
        self.assertEqual(self.get("/admin")[0], 403)

    def test_sessions_survive_a_server_restart(self):
        """Sessions live in the database, so a redeploy doesn't sign the
        whole club out mid-season."""
        self.login_as(self.al, self.al_pin)
        _, before, _ = self.get("/me")
        self.assertIn("Al", before)

        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        status, after, _ = self.get("/me")
        self.assertEqual(status, 200)
        self.assertIn("Al", after)

    def test_a_forged_csrf_token_changes_nothing(self):
        self.login_admin()
        self.post("/admin/player", {"csrf": "forged", "action": "add",
                                    "name": "Sneaky Player"})
        self.assertIsNone(self.db.find_player_by_name("Sneaky Player"))

    def test_a_missing_csrf_token_changes_nothing(self):
        self.login_as(self.al, self.al_pin)
        self.post("/submit", {"division": MS, "a1": self.al.id, "b1": self.bo.id,
                              "score": "6-0 6-0", "played_on": days_ago(1)})
        self.assertEqual(self.db.list_matches(), [])


class TestClaimingAnAccount(WebTestCase):
    """Players choose their own PIN the first time they sign in.

    A randomly generated 4-digit PIN is not something anyone remembers for a
    credential they use twice a month, and it puts the captain in the business
    of handing out secrets. This flow removes both problems.
    """

    def setUp(self):
        super().setUp()
        # Added the way admin adds them: no PIN at all.
        self.newbie = self.app.service.add_player("Newbie", category=div.MENS)

    def test_a_new_player_has_no_pin(self):
        self.assertFalse(self.newbie.pin_set)
        self.assertFalse(self.db.has_pin(self.newbie.id))

    def test_signing_in_without_a_pin_offers_to_create_one(self):
        _, body, url = self.post("/login", {
            "csrf": self.csrf(), "player_id": self.newbie.id, "pin": ""})
        self.assertIn("/claim", url)
        self.assertIn("Choose a 4-digit PIN", body)
        self.assertIn("Newbie", body)

    def test_choosing_a_pin_sets_it_and_signs_them_in(self):
        self.post("/login", {"csrf": self.csrf(), "player_id": self.newbie.id,
                             "pin": ""})
        _, body, _ = self.post("/claim", {
            "csrf": self.csrf(), "player_id": self.newbie.id,
            "pin": "2580", "pin2": "2580"})
        self.assertTrue(self.flashes(body, "ok"))
        self.assertTrue(self.db.check_pin(self.newbie.id, "2580"))
        # And they're signed in already -- no second login step.
        _, me, _ = self.get("/me")
        self.assertIn("Newbie", me)

    def test_mismatched_pins_are_rejected(self):
        _, body, _ = self.post("/claim", {
            "csrf": self.csrf(), "player_id": self.newbie.id,
            "pin": "2580", "pin2": "1111"})
        self.assertTrue(self.flashes(body, "err"))
        self.assertFalse(self.db.has_pin(self.newbie.id))

    def test_a_pin_that_is_not_four_digits_is_rejected(self):
        for bad in ("12", "abcd", "123456"):
            _, body, _ = self.post("/claim", {
                "csrf": self.csrf(), "player_id": self.newbie.id,
                "pin": bad, "pin2": bad})
            self.assertTrue(self.flashes(body, "err"), bad)
            self.assertFalse(self.db.has_pin(self.newbie.id), bad)

    def test_an_account_that_already_has_a_pin_cannot_be_reclaimed(self):
        taken, _ = self.add("Taken", pin="4321")
        _, body, _ = self.post("/claim", {
            "csrf": self.csrf(), "player_id": taken.id,
            "pin": "0000", "pin2": "0000"})
        self.assertTrue(self.flashes(body, "err"))
        self.assertTrue(self.db.check_pin(taken.id, "4321"))

    def test_after_an_admin_clears_it_they_choose_again(self):
        self.db.set_pin(self.newbie.id, "1111")
        self.db.clear_pin(self.newbie.id)
        _, _, url = self.post("/login", {
            "csrf": self.csrf(), "player_id": self.newbie.id, "pin": "1111"})
        self.assertIn("/claim", url)
        self.post("/claim", {"csrf": self.csrf(), "player_id": self.newbie.id,
                             "pin": "9999", "pin2": "9999"})
        self.assertTrue(self.db.check_pin(self.newbie.id, "9999"))
        self.assertFalse(self.db.check_pin(self.newbie.id, "1111"))

    def test_the_login_page_tells_first_timers_what_to_do(self):
        _, body, _ = self.get("/login")
        self.assertIn("First time?", body)


class TestSettingsPage(WebTestCase):
    config_overrides = {"smtp_host": "smtp.example.edu",
                        "smtp_from": "ladder@example.edu",
                        "base_url": "https://ladder.example.edu"}

    def setUp(self):
        super().setUp()
        self.roster()
        self.login_as(self.al, self.al_pin)

    def test_a_player_can_set_their_email_and_toggles(self):
        self.post("/me", {"csrf": self.csrf("/me"), "email": "al@example.edu",
                          "notify_confirm": "1", "notify_weekly": "1"})
        player = self.db.get_player(self.al.id)
        self.assertEqual(player.email, "al@example.edu")
        self.assertTrue(player.notify_confirm)
        self.assertTrue(player.notify_weekly)
        # Unticked boxes are turned off, not left alone.
        self.assertFalse(player.notify_result)
        self.assertFalse(player.notify_season)

    def test_a_player_can_change_their_own_pin(self):
        self.post("/me/pin", {"csrf": self.csrf("/me"), "pin": "4321"})
        self.assertTrue(self.db.check_pin(self.al.id, "4321"))

    def test_a_bad_pin_is_refused(self):
        _, body, _ = self.post("/me/pin", {"csrf": self.csrf("/me"), "pin": "12"})
        self.assertTrue(self.flashes(body, "err"))
        self.assertTrue(self.db.check_pin(self.al.id, self.al_pin))

    def test_the_unsubscribe_link_turns_off_exactly_one_type(self):
        from ladder.mailer import unsubscribe_token
        token = unsubscribe_token(self.config.secret_key, self.al.id, "weekly")
        self.db.set_notification(self.al.id, "weekly", True)
        status, body, _ = self.get(
            f"/unsubscribe?p={self.al.id}&k=weekly&t={token}")
        self.assertEqual(status, 200)
        self.assertIn("Unsubscribed", body)
        player = self.db.get_player(self.al.id)
        self.assertFalse(player.notify_weekly)
        self.assertTrue(player.notify_confirm)      # others untouched

    def test_a_forged_unsubscribe_link_is_rejected(self):
        status, _, _ = self.get(f"/unsubscribe?p={self.al.id}&k=weekly&t=forged")
        self.assertEqual(status, 400)
        self.assertTrue(self.db.get_player(self.al.id).notify_confirm)


class TestAdmin(WebTestCase):
    def setUp(self):
        super().setUp()
        self.roster()
        self.login_admin()

    def test_a_new_player_arrives_with_no_pin_to_hand_out(self):
        _, body, _ = self.post("/admin/player", {
            "csrf": self.csrf("/admin"), "action": "add",
            "name": "Cara Nunes", "category": div.WOMENS})
        player = self.db.find_player_by_name("Cara Nunes")
        self.assertIsNotNone(player)
        self.assertEqual(player.category, div.WOMENS)
        self.assertFalse(player.pin_set)
        self.assertFalse(self.db.has_pin(player.id))
        # Nothing secret is shown, because nothing was generated.
        self.assertNotRegex(body, r"PIN is <b>\d{4}</b>")

    def test_clearing_a_pin_sends_the_player_back_to_choosing_one(self):
        self.assertTrue(self.db.has_pin(self.al.id))
        self.post("/admin/player", {"csrf": self.csrf("/admin"),
                                    "action": "clearpin",
                                    "player_id": self.al.id})
        self.assertFalse(self.db.has_pin(self.al.id))
        self.assertFalse(self.db.check_pin(self.al.id, self.al_pin))

    def test_the_players_table_shows_who_has_signed_in(self):
        self.app.service.add_player("Never Signedin", category=div.MENS)
        _, body, _ = self.get("/admin")
        self.assertIn("not signed in yet", body)
        self.assertIn("PIN set", body)

    def test_changing_a_category_takes_effect(self):
        self.post("/admin/player", {"csrf": self.csrf("/admin"),
                                    "action": "category",
                                    "player_id": self.al.id,
                                    "category": div.WOMENS})
        self.assertEqual(self.db.get_player(self.al.id).category, div.WOMENS)

    def test_starting_a_season_from_the_admin_page(self):
        self.post("/admin/season", {"csrf": self.csrf("/admin"),
                                    "action": "start", "name": "Spring 2027"})
        seasons = self.db.seasons()
        self.assertEqual(len(seasons), 2)
        self.assertEqual(seasons[-1].name, "Spring 2027")
        self.assertTrue(seasons[-1].is_current)

    def test_csv_import_reports_good_and_bad_rows(self):
        _, body, _ = self.post("/admin/import", {
            "csrf": self.csrf("/admin"), "action": "import",
            "text": ("date,division,player_a,player_a2,player_b,player_b2,score\n"
                     f"{days_ago(9)},mens_singles,Al,,Bo,,6-1 6-2\n"
                     f"{days_ago(8)},mens_doubles,Al,Bo,Cy,Dan,6-2 6-2\n"
                     f"{days_ago(7)},mens_singles,Al,,Bo,,nonsense\n")})
        self.assertTrue(self.flashes(body, "ok"))
        self.assertTrue(self.flashes(body, "err"))
        self.assertEqual(len(self.db.confirmed_matches_chronological()), 2)

    def test_deleting_a_match_recalculates_the_ladder(self):
        self.app.service.submit_result(
            division=MS, side_a=[self.al.id], side_b=[self.bo.id],
            score_text="6-0 6-0", played_on=days_ago(5), auto_confirm=True)
        match_id = self.db.list_matches()[0].id
        self.post("/admin/match", {"csrf": self.csrf("/admin"),
                                   "action": "delete", "match_id": match_id})
        self.assertIsNone(self.db.get_match(match_id))
        self.assertEqual(self.app.service.engine.ladder(MS).entries, [])

    def test_deactivating_removes_a_player_from_the_ladder(self):
        self.app.service.submit_result(
            division=MS, side_a=[self.al.id], side_b=[self.bo.id],
            score_text="6-0 6-0", played_on=days_ago(5), auto_confirm=True)
        self.post("/admin/player", {"csrf": self.csrf("/admin"),
                                    "action": "toggle", "player_id": self.al.id})
        self.assertFalse(self.db.get_player(self.al.id).active)
        self.assertIsNone(self.app.service.engine.ladder(MS).entry(self.al.id))


class TestAvailabilityPages(WebTestCase):
    def setUp(self):
        super().setUp()
        self.roster()
        self.login_as(self.al, self.al_pin)

    def test_saving_the_grid_stores_a_merged_week(self):
        payload = [("csrf", self.csrf("/availability")),
                   ("slot", "1-900-960"), ("slot", "1-960-1020"),
                   ("slot", "3-540-600")]
        body = urllib.parse.urlencode(payload).encode()
        self.opener.open(urllib.request.Request(
            self.base + "/availability", data=body))
        weekly = self.db.get_availability(self.al.id).weekly
        self.assertEqual(weekly[1], [(900, 1020)])      # contiguous merged
        self.assertEqual(weekly[3], [(540, 600)])

    def test_saving_an_empty_grid_clears_the_week(self):
        self.db.set_weekly_availability(self.al.id, {1: [(900, 1020)]})
        self.post("/availability", {"csrf": self.csrf("/availability")})
        self.assertEqual(self.db.get_availability(self.al.id).weekly, {})

    def test_blocking_a_day_leaves_the_pattern_alone(self):
        self.db.set_weekly_availability(self.al.id, {d: [(900, 1080)]
                                                    for d in range(7)})
        target = (date.today() + timedelta(days=2)).isoformat()
        self.post("/availability/day", {"csrf": self.csrf("/availability"),
                                        "on_date": target, "action": "block"})
        avail = self.db.get_availability(self.al.id)
        self.assertEqual(avail.on(date.fromisoformat(target)), [])
        self.assertEqual(avail.weekly[0], [(900, 1080)])     # pattern intact

    def test_undoing_a_block_restores_the_day(self):
        self.db.set_weekly_availability(self.al.id, {d: [(900, 1080)]
                                                    for d in range(7)})
        target = (date.today() + timedelta(days=2)).isoformat()
        csrf = self.csrf("/availability")
        self.post("/availability/day", {"csrf": csrf, "on_date": target,
                                        "action": "block"})
        self.post("/availability/day", {"csrf": csrf, "on_date": target,
                                        "action": "restore"})
        self.assertEqual(
            self.db.get_availability(self.al.id).on(date.fromisoformat(target)),
            [(900, 1080)])

    def test_anonymous_visitors_are_sent_to_sign_in(self):
        self.get("/logout")
        _, _, url = self.get("/availability")
        self.assertIn("/login", url)


class TestSchedulingPages(WebTestCase):
    def setUp(self):
        super().setUp()
        self.roster()
        # Al and Bo overlap on the same weekday.
        for pid in (self.al.id, self.bo.id):
            self.db.set_weekly_availability(pid, {d: [(900, 1200)]
                                                  for d in range(7)})
        self.login_as(self.al, self.al_pin)

    def test_the_find_page_offers_real_overlapping_times(self):
        _, body, _ = self.get(f"/find/{self.bo.id}")
        self.assertIn("Suggested times", body)
        self.assertIn('name="starts_at"', body)
        self.assertNotIn("No overlap", body)

    def test_it_says_so_when_nobody_has_set_availability(self):
        _, body, _ = self.get(f"/find/{self.cy.id}")
        self.assertIn("availability", body)
        self.assertIn("No overlap", body)

    def test_sending_and_accepting_a_request(self):
        when = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT16:00")
        _, body, _ = self.post("/request", {
            "csrf": self.csrf("/schedule"), "opponent": self.bo.id,
            "division": MS, "match_format": "one_set", "starts_at": when,
            "message": "courts free"})
        self.assertTrue(self.flashes(body, "ok"))
        request = self.db.list_match_requests(player_id=self.al.id)[0]
        self.assertEqual(request.status, "pending")

        self.get("/logout")
        self.login_as(self.bo, self.bo_pin)
        _, body, _ = self.post("/request/respond", {
            "csrf": self.csrf("/schedule"), "request_id": request.id,
            "action": "accept"})
        self.assertTrue(self.flashes(body, "ok"))
        self.assertEqual(self.db.get_match_request(request.id).status, "accepted")

    def test_a_request_in_the_past_is_refused(self):
        when = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT16:00")
        _, body, _ = self.post("/request", {
            "csrf": self.csrf("/schedule"), "opponent": self.bo.id,
            "division": MS, "match_format": "one_set", "starts_at": when})
        self.assertTrue(self.flashes(body, "err"))
        self.assertEqual(self.db.list_match_requests(), [])

    def test_the_schedule_page_nudges_you_to_set_availability(self):
        self.get("/logout")
        self.login_as(self.cy, self.cy_pin)          # Cy has none
        _, body, _ = self.get("/schedule")
        self.assertIn("set your availability", body)
        self.assertIn('href="/availability"', body)


class TestTournamentPages(WebTestCase):
    def setUp(self):
        super().setUp()
        self.roster()
        for day in (30, 25, 20):
            self.app.service.submit_result(
                division=MS, side_a=[self.al.id], side_b=[self.bo.id],
                score_text="6-2", played_on=days_ago(day), auto_confirm=True)

    def create(self, style="elimination"):
        self.login_admin()
        payload = [("csrf", self.csrf("/admin")), ("action", "create"),
                   ("name", "Fall Open"), ("division", MS), ("style", style),
                   ("seeding", "ladder"), ("match_format", "one_set"),
                   ("round_days", "7")]
        payload += [("entrant", str(p.id))
                    for p in (self.al, self.bo, self.cy, self.dan)]
        body = urllib.parse.urlencode(payload).encode()
        return self.opener.open(urllib.request.Request(
            self.base + "/admin/tournament", data=body)).read().decode()

    def test_an_admin_can_create_a_tournament(self):
        self.create()
        events = self.db.list_tournaments()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].name, "Fall Open")
        self.assertEqual(len(self.db.entries(events[0].id)), 4)

    def test_the_bracket_renders(self):
        self.create()
        tournament = self.db.list_tournaments()[0]
        status, body, _ = self.get(f"/tournament/{tournament.id}")
        self.assertEqual(status, 200)
        self.assertIn("Draw", body)
        self.assertIn("Semi-finals", body)

    def test_a_round_robin_shows_standings_instead(self):
        self.create(style="round_robin")
        tournament = self.db.list_tournaments()[0]
        _, body, _ = self.get(f"/tournament/{tournament.id}")
        self.assertIn("Standings", body)
        self.assertNotIn("<h2>Draw</h2>", body)

    def test_the_tournament_list_shows_it(self):
        self.create()
        _, body, _ = self.get("/tournaments")
        self.assertIn("Fall Open", body)

    def test_a_non_admin_cannot_create_one(self):
        self.login_as(self.al, self.al_pin)
        payload = [("csrf", self.csrf("/schedule")), ("action", "create"),
                   ("name", "Sneaky Cup"), ("division", MS),
                   ("style", "elimination"), ("seeding", "ladder"),
                   ("match_format", "one_set")]
        payload += [("entrant", str(self.al.id)), ("entrant", str(self.bo.id))]
        self.opener.open(urllib.request.Request(
            self.base + "/admin/tournament",
            data=urllib.parse.urlencode(payload).encode()))
        self.assertEqual(self.db.list_tournaments(), [])

    def test_overdue_matches_are_flagged_to_the_admin(self):
        self.create()
        tournament = self.db.list_tournaments()[0]
        self.db.set_round_deadline(tournament.id, 0, days_ago(3))
        _, body, _ = self.get(f"/tournament/{tournament.id}")
        self.assertIn("past the round", body)
        self.assertIn("advances", body)

    def test_unknown_tournaments_are_404(self):
        self.assertEqual(self.get("/tournament/999")[0], 404)


class TestPlayerPage(WebTestCase):
    def setUp(self):
        super().setUp()
        self.roster()

    def test_the_rating_chart_is_drawn_once_there_is_history(self):
        for week in range(8):
            self.app.service.submit_result(
                division=MS, side_a=[self.al.id], side_b=[self.bo.id],
                score_text="6-4 6-4", played_on=days_ago(60 - week * 7),
                auto_confirm=True)
        status, body, _ = self.get(f"/player/{self.al.id}")
        self.assertEqual(status, 200)
        for element in ("<svg", "<polyline", "<polygon", "viewBox"):
            self.assertIn(element, body)

    def test_the_page_shows_a_card_per_division_played(self):
        self.app.service.submit_result(
            division=MS, side_a=[self.al.id], side_b=[self.bo.id],
            score_text="6-4 6-4", played_on=days_ago(9), auto_confirm=True)
        self.app.service.submit_result(
            division=MD, side_a=[self.al.id, self.bo.id],
            side_b=[self.cy.id, self.dan.id], score_text="6-4 6-4",
            played_on=days_ago(8), auto_confirm=True)
        _, body, _ = self.get(f"/player/{self.al.id}")
        self.assertIn("Men&#x27;s Singles", body)
        self.assertIn("Men&#x27;s Doubles", body)

    def test_the_partner_table_appears_for_doubles_players(self):
        for day in (9, 6, 3):
            self.app.service.submit_result(
                division=MD, side_a=[self.al.id, self.bo.id],
                side_b=[self.cy.id, self.dan.id], score_text="6-2 6-2",
                played_on=days_ago(day), auto_confirm=True)
        _, body, _ = self.get(f"/player/{self.al.id}")
        self.assertIn("Partners", body)
        self.assertIn("vs expected", body)
        self.assertIn("Bo", body)

    def test_scores_are_shown_from_the_winners_side(self):
        # Stored from side A's view, but side B won.
        self.app.service.submit_result(
            division=MS, side_a=[self.al.id], side_b=[self.bo.id],
            score_text="6-7 1-6", played_on=days_ago(5), auto_confirm=True)
        _, body, _ = self.get("/matches")
        self.assertIn("7-6 6-1", body)
        self.assertNotIn(">6-7 1-6<", body)

    def test_a_player_with_no_matches_still_has_a_page(self):
        status, body, _ = self.get(f"/player/{self.al.id}")
        self.assertEqual(status, 200)
        self.assertIn("No confirmed matches", body)


if __name__ == "__main__":
    unittest.main()
