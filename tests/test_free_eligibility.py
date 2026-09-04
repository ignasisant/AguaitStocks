"""Who may spend the operator-funded free chain, and who may not."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from stocks.chat import engine


def _prefs(hours_old=None, *, estimated=False, email=None):
    prefs = {}
    if estimated:
        prefs["first_seen_estimated"] = True
    if hours_old is not None:
        prefs["first_seen"] = (
            datetime.now(UTC) - timedelta(hours=hours_old)).isoformat()
    if email:
        prefs["email"] = email
    return prefs


@pytest.fixture
def policy(monkeypatch):
    """Set the [free_llm] policy the way a deploy would."""
    def apply(**settings):
        monkeypatch.setattr(engine, "secret",
                            lambda env, section, key, default="":
                            settings.get(key, default))
    return apply


# ------------------------------------------------------------------ policies


def test_the_default_lets_a_brand_new_account_in(policy):
    # A user who just signed up can use the assistant in that first session:
    # being told to come back tomorrow is how a new account never comes back.
    policy()
    assert engine.free_policy() == "trial"
    assert engine.free_eligible(_prefs(0))
    assert engine.free_eligible(_prefs(1))


def test_open_lets_anyone_signed_in_through(policy):
    policy(eligibility="open")
    assert engine.free_eligible(_prefs(0))


def test_established_still_holds_a_young_account_out(policy):
    # The old hard wall is still an option a worried operator can set.
    policy(eligibility="established")
    assert not engine.free_eligible(_prefs(1))
    assert engine.free_eligible(_prefs(25))


def test_an_unknown_policy_falls_back_to_the_default(policy):
    policy(eligibility="whatever")
    assert engine.free_policy() == "trial"


def test_the_waiting_period_is_configurable(policy):
    policy(eligibility="established", min_account_hours="72")
    assert not engine.free_eligible(_prefs(48))
    assert engine.free_eligible(_prefs(80))


def test_a_nonsense_waiting_period_uses_the_default(policy):
    policy(eligibility="established", min_account_hours="soon")
    assert engine.free_eligible(_prefs(25))


# ------------------------------------------------------------- trial window


def test_a_new_account_spends_against_the_smaller_cap(policy):
    policy()
    assert engine.in_free_trial(_prefs(1))
    assert engine.free_daily_cap(_prefs(1)) == engine.FREE_TRIAL_CAP
    assert engine.free_daily_cap(_prefs(25)) == engine.FREE_DAILY_CAP
    # No account in hand means the configured cap, not the trial one.
    assert engine.free_daily_cap() == engine.FREE_DAILY_CAP


def test_the_trial_window_closes_on_the_same_clock_as_the_old_wall(policy):
    policy(min_account_hours="72")
    assert engine.in_free_trial(_prefs(48))
    assert not engine.in_free_trial(_prefs(80))


def test_the_trial_cap_is_configurable(policy):
    policy(trial_cap="2")
    assert engine.free_daily_cap(_prefs(1)) == 2


def test_a_nonsense_trial_cap_uses_the_default(policy):
    policy(trial_cap="loads")
    assert engine.free_daily_cap(_prefs(1)) == engine.FREE_TRIAL_CAP


def test_the_trial_cap_never_exceeds_the_configured_one(policy):
    # An operator who dialled the daily allowance below the trial one meant
    # that number as the ceiling for everybody.
    policy(daily_cap="3")
    assert engine.free_daily_cap(_prefs(1)) == 3


def test_only_the_trial_policy_has_a_trial(policy):
    policy(eligibility="open")
    assert not engine.in_free_trial(_prefs(1))
    assert engine.free_daily_cap(_prefs(1)) == engine.FREE_DAILY_CAP


def test_an_account_of_unknown_age_is_not_put_on_trial(policy):
    policy()
    assert not engine.in_free_trial(_prefs(0, estimated=True))
    assert not engine.in_free_trial({})


def test_a_new_account_runs_out_at_the_trial_cap_not_the_full_one(policy):
    policy()
    prefs = _prefs(1)
    for _ in range(engine.FREE_TRIAL_CAP):
        assert engine.spend_free_quota(prefs)
    assert not engine.spend_free_quota(prefs)
    assert engine.free_account_left(prefs) == 0


def test_the_trial_wall_says_the_allowance_grows(policy):
    policy()
    prefs = _prefs(1)
    prefs[f"free_msgs::{time.strftime('%Y-%m-%d')}"] = engine.FREE_TRIAL_CAP
    assert engine.free_cap_reason(prefs) == "trial"
    assert engine.FREE_CAP_ERRORS["trial"] == "chat.free_cap_trial"


# ---------------------------------------------------------------- allowlist


def test_the_allowlist_admits_only_its_addresses(policy):
    policy(eligibility="allowlist", allowed_emails="a@x.com, B@Y.com")
    assert engine.free_eligible(_prefs(0, email="a@x.com"))
    assert engine.free_eligible(_prefs(0, email="b@y.com"))  # case-insensitive
    assert not engine.free_eligible(_prefs(999, email="c@z.com"))
    assert not engine.free_eligible(_prefs(999))  # no address stamped yet


def test_an_empty_allowlist_is_a_misconfiguration_not_a_lockout(policy):
    # A list nobody is on would lock out the operator too. That is a missing
    # setting, not a decision to serve no one.
    policy(eligibility="allowlist", allowed_emails="  ")
    assert engine.free_eligible(_prefs(0))


def test_the_allowlist_accepts_the_separators_people_actually_type(policy):
    policy(eligibility="allowlist", allowed_emails="a@x.com; b@y.com,,c@z.com")
    assert engine.free_allowlist() == {"a@x.com", "b@y.com", "c@z.com"}


# ------------------------------------------------------------- account age


def test_an_account_that_predates_the_bookkeeping_counts_as_established(policy):
    # first_seen was backfilled at some later login, so it says nothing about
    # when the account started — and those accounts are old by definition.
    policy()
    assert engine.free_eligible(_prefs(0, estimated=True))
    assert engine.account_age_hours(_prefs(0, estimated=True)) is None


def test_an_account_with_no_stamp_at_all_is_not_punished_for_it(policy):
    policy()
    assert engine.free_eligible({})


def test_a_corrupt_stamp_does_not_deny_the_account(policy):
    policy()
    assert engine.free_eligible({"first_seen": "not a date"})


def test_a_naive_timestamp_is_read_as_utc():
    naive = (datetime.now(UTC) - timedelta(hours=10)).replace(tzinfo=None)
    age = engine.account_age_hours({"first_seen": naive.isoformat()})
    assert 9.5 < age < 10.5


# --------------------------------------------------------------- enforcement


def test_an_ineligible_account_is_not_offered_the_chain(policy, monkeypatch):
    policy(eligibility="established")
    assert all(p.id != "free" for p, _, _ in engine.attempts(_prefs(1)))


def test_an_eligible_account_still_gets_it(policy, monkeypatch):
    policy()
    import dataclasses

    from stocks.web import llm

    free = dataclasses.replace(llm.PROVIDERS["free"], _available=lambda: True)
    monkeypatch.setitem(llm.PROVIDERS, "free", free)
    assert any(p.id == "free" for p, _, _ in engine.attempts(_prefs(48)))


def test_the_quota_refuses_an_ineligible_account_outright(policy):
    # The gate is re-checked where the money is spent, so a saved provider
    # preference cannot walk around the provider list.
    policy(eligibility="established")
    prefs = _prefs(1)
    assert not engine.spend_free_quota(prefs)
    assert not [k for k in prefs if k.startswith("free_msgs::")]
    assert engine.free_cap_reason(prefs) == "ineligible"
    assert engine.FREE_CAP_ERRORS["ineligible"] == "chat.free_ineligible"


def test_byok_is_untouched_by_the_policy(policy, monkeypatch):
    # A user with their own key pays their own bill and needs no permission.
    policy(eligibility="allowlist", allowed_emails="nobody@example.com")
    monkeypatch.setattr(engine, "decrypt_byok", lambda prefs, pid: "sk-real")
    ids = [p.id for p, _, _ in engine.attempts(_prefs(0))]
    assert "anthropic" in ids and "free" not in ids
