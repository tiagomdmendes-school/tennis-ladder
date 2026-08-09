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

    def add(self, name, category=div.MENS, email=""):
        """Returns (player, plain_pin) -- the PIN is only readable at creation."""
        player = self.app.service.add_player(name, email, "", category)
        return player, getattr(player, "generated_pin")

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

    def test_adding_a_player_reveals_their_pin_once(self):
        _, body, _ = self.post("/admin/player", {
            "csrf": self.csrf("/admin"), "action": "add",
            "name": "Cara Nunes", "category": div.WOMENS})
        player = self.db.find_player_by_name("Cara Nunes")
        self.assertIsNotNone(player)
        self.assertEqual(player.category, div.WOMENS)
        shown = re.search(r"PIN is <b>(\d{4})</b>", body)
        self.assertIsNotNone(shown)
        self.assertTrue(self.db.check_pin(player.id, shown.group(1)))

    def test_resetting_a_pin_issues_a_new_working_one(self):
        _, body, _ = self.post("/admin/player", {
            "csrf": self.csrf("/admin"), "action": "resetpin",
            "player_id": self.al.id})
        shown = re.search(r"PIN is <b>(\d{4})</b>", body)
        self.assertIsNotNone(shown)
        self.assertTrue(self.db.check_pin(self.al.id, shown.group(1)))

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
