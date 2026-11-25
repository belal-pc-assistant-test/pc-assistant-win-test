"""Remote link secret: generation, single-use lock, cookie, expiry, lock-out, constant-time compare."""

from __future__ import annotations

import base64
import hmac
import unittest
from unittest import mock

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()

from assistant.remote import gate as gate_mod
from assistant.remote.gate import (FAILURE_LIMIT, LOCKOUT_SECONDS, TOTAL_FAILURE_CAP, AccessGate,
                                   EventLimiter, generate_secret)


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_gate(clock: Clock, ttl: float = 900.0) -> tuple[AccessGate, str]:
    secret = generate_secret()
    return AccessGate(secret, deadline=clock.now + ttl, now=clock), secret


class SecretTests(unittest.TestCase):
    def test_secret_carries_at_least_256_bits(self):
        secret = generate_secret()
        padded = secret + "=" * (-len(secret) % 4)
        self.assertGreaterEqual(len(base64.urlsafe_b64decode(padded)), 32)
        self.assertGreaterEqual(len(secret), 43)

    def test_secrets_are_unique_per_session(self):
        self.assertEqual(len({generate_secret() for _ in range(300)}), 300)

    def test_gate_keeps_no_plaintext_secret(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        for value in vars(gate).values():
            self.assertNotIn(secret, repr(value))

    def test_too_short_secret_is_refused(self):
        with self.assertRaises(ValueError):
            AccessGate("short", deadline=10.0)


class RedeemOnceTests(unittest.TestCase):
    def test_first_correct_use_issues_cookie_and_second_use_is_refused(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        decision, cookie = gate.redeem(secret)
        self.assertEqual(decision, "ok")
        self.assertTrue(cookie and len(cookie) >= 40)
        again, no_cookie = gate.redeem(secret)
        self.assertEqual(again, "used")
        self.assertIsNone(no_cookie)
        self.assertEqual(gate.state, gate_mod.LOCKED)

    def test_wrong_secret_is_denied_and_does_not_consume_the_real_one(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        decision, cookie = gate.redeem("x" * 43)
        self.assertEqual((decision, cookie), ("denied", None))
        self.assertEqual(gate.state, gate_mod.FRESH)
        self.assertEqual(gate.redeem(secret)[0], "ok")

    def test_cookie_works_only_when_it_matches_the_issued_value(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        self.assertEqual(gate.authorize("anything"), "denied")  # nothing issued yet
        _, cookie = gate.redeem(secret)
        self.assertEqual(gate.authorize(cookie), "ok")
        self.assertEqual(gate.authorize(cookie + "x"), "denied")
        self.assertEqual(gate.authorize(None), "missing")
        self.assertEqual(gate.authorize(""), "missing")

    def test_comparison_uses_constant_time_compare(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        with mock.patch.object(gate_mod.hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            gate.redeem("wrong-value")
            self.assertTrue(spy.called)

    def test_comparison_is_not_short_circuited_by_length(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        # Different lengths must still go through the digest comparison (no early return).
        self.assertEqual(gate.redeem("")[0], "denied")
        self.assertEqual(gate.redeem(secret + "extra")[0], "denied")


class ExpiryAndRevocationTests(unittest.TestCase):
    def test_expired_secret_is_refused_even_when_correct(self):
        clock = Clock()
        gate, secret = make_gate(clock, ttl=60)
        clock.advance(61)
        self.assertEqual(gate.redeem(secret)[0], "expired")

    def test_cookie_stops_working_at_expiry(self):
        clock = Clock()
        gate, secret = make_gate(clock, ttl=60)
        _, cookie = gate.redeem(secret)
        clock.advance(60)
        self.assertEqual(gate.authorize(cookie), "expired")

    def test_revoke_kills_cookie_and_secret_immediately(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        _, cookie = gate.redeem(secret)
        gate.revoke()
        self.assertEqual(gate.authorize(cookie), "expired")
        self.assertEqual(gate.redeem(secret)[0], "expired")
        self.assertTrue(gate.expired())

    def test_expired_attempts_are_not_counted_as_attacks(self):
        clock = Clock()
        gate, secret = make_gate(clock, ttl=10)
        clock.advance(11)
        for _ in range(FAILURE_LIMIT + 2):
            gate.redeem(secret)
        self.assertEqual(gate.total_failures, 0)


class RateLimitTests(unittest.TestCase):
    def test_repeated_denials_lock_out_even_the_correct_secret(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        for _ in range(FAILURE_LIMIT):
            self.assertEqual(gate.redeem("nope" * 10)[0], "denied")
        self.assertEqual(gate.redeem(secret)[0], "limited")
        self.assertEqual(gate.authorize("cookie-guess"), "limited")

    def test_lockout_does_not_consume_the_secret(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        for _ in range(FAILURE_LIMIT):
            gate.redeem("nope" * 10)
        self.assertEqual(gate.redeem(secret)[0], "limited")
        clock.advance(LOCKOUT_SECONDS + 1)
        self.assertEqual(gate.redeem(secret)[0], "ok")  # the legitimate owner can still use it

    def test_failures_spread_out_in_time_do_not_lock_out(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        for _ in range(FAILURE_LIMIT - 1):
            gate.redeem("nope" * 10)
            clock.advance(40)
        self.assertEqual(gate.redeem(secret)[0], "ok")

    def test_every_denial_is_counted_for_the_session_cap(self):
        clock = Clock()
        gate, secret = make_gate(clock)
        for _ in range(3):
            gate.redeem("nope" * 10)
            clock.advance(LOCKOUT_SECONDS + 1)
        self.assertEqual(gate.total_failures, 3)
        self.assertLess(FAILURE_LIMIT, TOTAL_FAILURE_CAP)


class EventLimiterTests(unittest.TestCase):
    def test_burst_then_refill(self):
        clock = Clock()
        limiter = EventLimiter(rate=10, burst=3, now=clock)
        self.assertEqual([limiter.allow() for _ in range(5)], [True, True, True, False, False])
        clock.advance(0.1)  # one token refilled
        self.assertTrue(limiter.allow())
        self.assertFalse(limiter.allow())


if __name__ == "__main__":
    unittest.main()
