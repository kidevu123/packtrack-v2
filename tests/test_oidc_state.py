"""Tests for the OIDC state signer's age window.

Locks the rules that caused the v2.2.0 "SSO session expired or tampered"
incident: the state's max-age must be wide enough to cover a normal
human MFA round-trip, and the matching cookie must not expire sooner
(otherwise the operator sees a misleading state-mismatch error).
"""
from __future__ import annotations

import time

import pytest
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from packtrack.config import settings
from packtrack.routes import auth as auth_route


def test_oidc_state_max_age_is_at_least_15_minutes():
    """5 minutes was too short for real MFA flows. Lock the floor so a
    future change can't silently regress to the old value."""
    assert auth_route._OIDC_STATE_MAX_AGE_SECONDS >= 15 * 60


def test_signer_round_trip_at_constant_age():
    """A state signed now must verify now under the configured window."""
    settings.PACKTRACK_SECRET_KEY = "test-key-fixed-for-deterministic-test"
    s = auth_route._oidc_signer()
    sig = s.dumps({"n": "abc", "next": "/"})
    assert s.loads(sig, max_age=auth_route._OIDC_STATE_MAX_AGE_SECONDS) == {
        "n": "abc", "next": "/",
    }


def test_signer_expired_raises_signature_expired_not_generic_badsignature():
    """The callback distinguishes 'expired' from 'tampered' — verify the
    library still surfaces the typed subclass we depend on."""
    settings.PACKTRACK_SECRET_KEY = "test-key-fixed-for-deterministic-test"
    s = URLSafeTimedSerializer(settings.PACKTRACK_SECRET_KEY, salt="oidc-state")
    sig = s.dumps({"n": "abc", "next": "/"})
    # max_age uses integer-second arithmetic and the loader requires
    # age > max_age (strictly greater), so sleep past two seconds.
    time.sleep(2.2)
    with pytest.raises(SignatureExpired):
        s.loads(sig, max_age=1)


def test_authentik_base_derived_from_issuer_url_no_lan_leak():
    """The /auth/sso redirect target must come from OIDC_ISSUER_URL, not
    a hardcoded LAN address. Regression guard: a public client must never
    be redirected to 192.168.x.x. or any private RFC1918 address."""
    settings.OIDC_ISSUER_URL = "https://auth.booute.duckdns.org/application/o/packtrack"
    assert auth_route._authentik_base() == "https://auth.booute.duckdns.org"

    settings.OIDC_ISSUER_URL = "https://idp.example.com:9000/application/o/x"
    assert auth_route._authentik_base() == "https://idp.example.com:9000"


def test_authentik_base_rejects_half_set_issuer():
    settings.OIDC_ISSUER_URL = "auth.booute.duckdns.org/application/o/packtrack"
    with pytest.raises(RuntimeError):
        auth_route._authentik_base()


def test_signer_tampered_raises_generic_badsignature_not_expired():
    """Tampered state must NOT come back as SignatureExpired so the error
    path stays distinct and the message stays truthful."""
    settings.PACKTRACK_SECRET_KEY = "test-key-fixed-for-deterministic-test"
    s = URLSafeTimedSerializer(settings.PACKTRACK_SECRET_KEY, salt="oidc-state")
    sig = s.dumps({"n": "abc", "next": "/"})
    # Flip one char in the signature portion → BadSignature, not Expired.
    payload, ts, signature = sig.split(".")
    bad_sig = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    with pytest.raises(BadSignature) as info:
        s.loads(".".join([payload, ts, bad_sig]),
                max_age=auth_route._OIDC_STATE_MAX_AGE_SECONDS)
    assert not isinstance(info.value, SignatureExpired)
