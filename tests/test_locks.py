import datetime as dt
import os
import unittest

from helpers import IsolatedTestCase, dead_pid
from lightsout import clock, state


class LimitTests(IsolatedTestCase):
    def test_none_without_file(self):
        self.assertIsNone(state.limit_until())

    def test_set_and_expire(self):
        t = clock.now() + dt.timedelta(hours=2)
        state.set_limit(t, "job", "detail")
        self.assertEqual(state.limit_until(), t.replace(microsecond=0))
        self.at("2026-10-19T02:00")
        self.assertIsNone(state.limit_until())

    def test_never_shortens_existing_limit(self):
        late = clock.now() + dt.timedelta(hours=5)
        state.set_limit(late, "a", "")
        state.set_limit(clock.now() + dt.timedelta(hours=1), "b", "")
        self.assertEqual(state.limit_until(), late.replace(microsecond=0))


class JsonTests(IsolatedTestCase):
    def test_roundtrip_and_corrupt(self):
        p = self.tmp / "x" / "y.json"
        state.write_json(p, {"a": 1})
        self.assertEqual(state.read_json(p), {"a": 1})
        p.write_text("{not json")
        self.assertIsNone(state.read_json(p))
        self.assertIsNone(state.read_json(self.tmp / "missing.json"))

    def test_job_state_merge(self):
        state.update_job_state("j", a=1)
        state.update_job_state("j", b=2)
        self.assertEqual(state.job_state("j"), {"a": 1, "b": 2})
        self.assertEqual(state.job_state("never"), {})

    def test_pid_alive(self):
        self.assertTrue(state.pid_alive(os.getpid()))
        self.assertFalse(state.pid_alive(dead_pid()))
        self.assertFalse(state.pid_alive(0))
        self.assertFalse(state.pid_alive("garbage"))


class LockTests(IsolatedTestCase):
    def test_exclusive(self):
        a = state.Lock("slot", "A").acquire()
        try:
            with self.assertRaises(state.Busy) as cm:
                state.Lock("slot", "B").acquire()
            self.assertEqual(cm.exception.holder["owner"], "A")
        finally:
            a.release()
        state.Lock("slot", "B").acquire().release()

    def test_slot_path_and_name_sanitised(self):
        self.assertEqual(state.lock_path("slot").name, "slot.json")
        p = state.lock_path("dir:/a b/c")
        self.assertEqual(p.parent.name, "locks")
        self.assertNotIn("/", p.name)

    def test_context_manager_releases(self):
        with state.Lock("x", "me"):
            self.assertIsNotNone(state.holder("x"))
        self.assertIsNone(state.holder("x"))

    def test_release_does_not_remove_someone_elses_lock(self):
        a = state.Lock("x", "A").acquire()
        state.lock_path("x").unlink()
        b = state.Lock("x", "B").acquire()
        a.release()
        self.assertEqual(state.holder("x")["owner"], "B")
        b.release()

    def test_stale_when_pid_dead(self):
        pid = dead_pid()
        state.write_json(state.lock_path("slot"), {"owner": "ghost", "pid": pid, "host": state.HOST,
                                                    "heartbeat": clock.iso(clock.now())})
        self.assertIsNone(state.holder("slot"))
        self.assertFalse(state.lock_path("slot").exists())
        state.Lock("slot", "new").acquire().release()

    def test_other_host_pid_not_judged_by_pid(self):
        state.write_json(state.lock_path("slot"), {"owner": "far", "pid": dead_pid(), "host": "other-host",
                                                    "heartbeat": clock.iso(clock.now())})
        self.assertEqual(state.holder("slot")["owner"], "far")

    def test_stale_by_old_heartbeat(self):
        old = clock.now() - dt.timedelta(minutes=state.STALE_MIN + 1)
        state.write_json(state.lock_path("slot"), {"owner": "slow", "pid": os.getpid(), "host": state.HOST,
                                                    "heartbeat": clock.iso(old)})
        self.assertIsNone(state.holder("slot"))

    def test_fresh_heartbeat_is_live(self):
        recent = clock.now() - dt.timedelta(minutes=state.STALE_MIN - 5)
        state.write_json(state.lock_path("slot"), {"owner": "ok", "pid": os.getpid(), "host": state.HOST,
                                                    "heartbeat": clock.iso(recent)})
        self.assertEqual(state.holder("slot")["owner"], "ok")

    def test_corrupt_lock_is_stale(self):
        p = state.lock_path("slot")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("garbage")
        self.assertIsNone(state.holder("slot"))


class HandLockTests(IsolatedTestCase):
    def test_hand_lock_blocks_then_expires(self):
        state.Lock("dir:/x", "hand", minutes=30).acquire()
        with self.assertRaises(state.Busy):
            state.Lock("dir:/x", "job:a").acquire()
        self.at("2026-10-18T23:36")
        self.assertIsNone(state.holder("dir:/x"))
        state.Lock("dir:/x", "job:a").acquire().release()

    def test_hand_lock_has_no_pid_and_survives_dead_process(self):
        state.Lock("dir:/x", "hand", minutes=30).acquire()
        d = state.holder("dir:/x")
        self.assertEqual(d["pid"], 0)
        self.assertIn("expires", d)

    def test_renew_own_hand_lock_extends_and_keeps_started(self):
        state.Lock("dir:/x", "hand", minutes=10).acquire()
        started = state.holder("dir:/x")["started"]
        self.at("2026-10-18T23:12")
        state.Lock("dir:/x", "hand", minutes=60).acquire()
        d = state.holder("dir:/x")
        self.assertEqual(d["started"], started)
        self.assertEqual(clock.parse_iso(d["expires"]), clock.now() + dt.timedelta(minutes=60))

    def test_release_by_owner(self):
        state.Lock("dir:/x", "hand", minutes=30).acquire()
        self.assertFalse(state.release_by_owner("dir:/x", "someone-else"))
        self.assertIsNotNone(state.holder("dir:/x"))
        self.assertTrue(state.release_by_owner("dir:/x", "hand"))
        self.assertIsNone(state.holder("dir:/x"))
        self.assertFalse(state.release_by_owner("dir:/x", "hand"))


if __name__ == "__main__":
    unittest.main()
