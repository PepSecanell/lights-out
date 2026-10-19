import datetime as dt
import json
import unittest
from zoneinfo import ZoneInfo

from helpers import IsolatedTestCase
from lightsout import classify as C

UTC = dt.timezone.utc


def now(s="2026-10-18T20:00"):
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


class ClassifyTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(C.classify("all good", exit_code=0).kind, C.OK)

    def test_ok_reply_with_scary_words_is_not_an_error(self):
        v = C.classify("the request timed out and I hit your usage limit joke", exit_code=0, is_error=False)
        self.assertEqual(v.kind, C.OK)

    def test_usage_limit_with_reset(self):
        v = C.classify("You've hit your session limit · resets 11:30pm (Europe/Madrid)", exit_code=1,
                       api_status=429, now=now())
        self.assertEqual(v.kind, C.LIMIT)
        self.assertIsNotNone(v.resume_after)

    def test_usage_limit_unreadable_waits_an_hour(self):
        n = now()
        v = C.classify("usage limit reached", exit_code=1, now=n)
        self.assertEqual(v.kind, C.LIMIT)
        self.assertEqual(v.resume_after, n + dt.timedelta(minutes=C.LIMIT_UNKNOWN_WAIT_MIN))

    def test_429_with_resets_is_limit_but_plain_429_is_transient(self):
        self.assertEqual(C.classify("slow down, resets 4am", exit_code=1, api_status=429, now=now()).kind, C.LIMIT)
        self.assertEqual(C.classify("slow down", exit_code=1, api_status=429).kind, C.TRANSIENT)

    def test_auth(self):
        for text in ("Failed to authenticate: OAuth session expired", "Not logged in", "please run /login",
                     "Invalid API key"):
            self.assertEqual(C.classify(text, exit_code=1).kind, C.AUTH, text)
        self.assertEqual(C.classify("nope", exit_code=1, api_status=401).kind, C.AUTH)

    def test_transient(self):
        for text in ("API Error: 500 Internal server error", "Overloaded", "ECONNRESET", "fetch failed",
                     "getaddrinfo ENOTFOUND", "connection refused"):
            self.assertEqual(C.classify(text, exit_code=1).kind, C.TRANSIENT, text)
        self.assertEqual(C.classify("boom", exit_code=1, api_status=503).kind, C.TRANSIENT)

    def test_failure(self):
        v = C.classify("the tests still fail", exit_code=1)
        self.assertEqual(v.kind, C.FAILURE)
        self.assertIn("tests still fail", v.detail)

    def test_failure_empty_text_mentions_code(self):
        self.assertIn("7", C.classify("", exit_code=7).detail)

    def test_timeout_is_failure_even_with_exit_zero(self):
        self.assertEqual(C.classify("", exit_code=0, timed_out=True).kind, C.FAILURE)

    def test_limit_beats_auth_and_transient(self):
        self.assertEqual(C.classify("Overloaded. You've hit your session limit", exit_code=1).kind, C.LIMIT)

    def test_scan_plain_command(self):
        self.assertEqual(C.scan("anything", 0).kind, C.OK)
        self.assertEqual(C.scan("x\nECONNRESET\ny", 1).kind, C.TRANSIENT)
        self.assertEqual(C.scan("a\nb\nc\nd\nboom", 2).detail, "c\nd\nboom")
        self.assertEqual(C.scan("hit your weekly limit, resets 9am", 1, now=now()).kind, C.LIMIT)

    def test_verdict_as_dict(self):
        d = C.Verdict(C.OK).as_dict()
        self.assertEqual(d["kind"], "ok")
        self.assertIsNone(d["resume_after"])


class ParseResetTests(unittest.TestCase):
    def test_none_when_no_reset(self):
        self.assertIsNone(C.parse_reset("nothing here", now()))
        self.assertIsNone(C.parse_reset("", now()))

    def test_time_with_zone_converted_back(self):
        n = now("2026-10-18T20:00")                    # 22:00 in Madrid (CEST, UTC+2)
        t = C.parse_reset("resets 11:30pm (Europe/Madrid)", n)
        madrid = t.astimezone(ZoneInfo("Europe/Madrid"))
        self.assertEqual((madrid.day, madrid.hour, madrid.minute), (18, 23, 30))
        self.assertEqual(t.tzinfo, n.tzinfo)

    def test_rolls_to_next_day_when_passed(self):
        n = now("2026-10-18T22:30")                    # 00:30 Madrid on the 19th
        t = C.parse_reset("resets 11:30pm (Europe/Madrid)", n)
        self.assertEqual(t.astimezone(ZoneInfo("Europe/Madrid")).day, 19)
        self.assertGreater(t, n)

    def test_hour_only_and_noon_midnight(self):
        n = now("2026-10-18T01:00")
        self.assertEqual(C.parse_reset("resets 4am", n).hour, 4)
        self.assertEqual(C.parse_reset("resets 12am", n).hour, 0)
        self.assertEqual(C.parse_reset("resets 12pm", n).hour, 12)

    def test_date_form(self):
        t = C.parse_reset("resets Oct 21, 9am", now())
        self.assertEqual((t.month, t.day, t.hour, t.minute), (10, 21, 9, 0))

    def test_date_form_wraps_to_next_year(self):
        t = C.parse_reset("resets Jan 2, 9am", now("2026-12-30T10:00"))
        self.assertEqual((t.year, t.month, t.day), (2027, 1, 2))

    def test_narrow_nbsp_before_ampm(self):
        t = C.parse_reset("resets 11:30 pm (Europe/Madrid)", now())
        self.assertEqual(t.astimezone(ZoneInfo("Europe/Madrid")).hour, 23)
        t = C.parse_reset("resets Oct 21, 9 am", now())
        self.assertEqual((t.day, t.hour), (21, 9))

    def test_unknown_zone_falls_back_to_local(self):
        t = C.parse_reset("resets 11pm (Nowhere/Land)", now())
        self.assertEqual(t.hour, 23)

    def test_limit_adds_margin(self):
        v = C.classify("hit your limit resets 11pm", exit_code=1, now=now("2026-10-18T10:00"))
        self.assertEqual((v.resume_after.hour, v.resume_after.minute), (23, C.LIMIT_MARGIN_MIN))


class ReadResultTests(IsolatedTestCase):
    def test_reads_last_result_and_stray_text(self):
        p = self.tmp / "log"
        p.write_text(json.dumps({"type": "system"}) + "\n"
                     + json.dumps({"type": "result", "is_error": True, "result": "bad", "session_id": "s1",
                                   "num_turns": 4, "total_cost_usd": 0.5, "api_error_status": 500}) + "\n"
                     + "stray line\n")
        r = C.read_result(p)
        self.assertEqual((r["session_id"], r["turns"], r["cost_usd"], r["api_status"]), ("s1", 4, 0.5, 500))
        self.assertTrue(r["is_error"])
        self.assertEqual(r["stray_text"], "stray line")

    def test_missing_file(self):
        self.assertEqual(C.read_result(self.tmp / "none"), {})

    def test_no_result_event(self):
        p = self.tmp / "log"
        p.write_text("Failed to authenticate\n")
        r = C.read_result(p)
        self.assertNotIn("is_error", r)
        self.assertIn("Failed to authenticate", r["stray_text"])


if __name__ == "__main__":
    unittest.main()
