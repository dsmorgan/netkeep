"""The status roll-up the chips and the menu bar read (#761), as pure functions of their
reads. Each case of ``frontend/src/features/system-status/roll-up.test.ts`` before #761
is here, against the server's words; times stay ``{placeholders}`` the clients fill."""

# The UI's words, typographic apostrophes and the Settings arrow included.
# ruff: noqa: RUF001

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from netkeep.models import MailboxArm, MailboxStatus, SyncRunKind, SyncRunTrigger
from netkeep.services import attention as attention_service
from netkeep.services import poll_status as poll_service
from netkeep.services import system_status as service
from netkeep.services.system_status import (
    FAILED,
    Chip,
    HeatRead,
    LinkedInRead,
    MailboxRead,
    Part,
    PrefillsRead,
    Reads,
    RunRead,
    ScheduleRead,
    Time,
    TimeStyle,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
State = poll_service.CheckState


def check(**overrides: Any) -> poll_service.Check:
    fields: dict[str, Any] = {
        "key": poll_service.GMAIL_REPLIES,
        "group": poll_service.CheckGroup.GMAIL,
        "label": "Gmail replies",
        "state": State.SCHEDULED,
        "interval": timedelta(minutes=10),
        "last_at": NOW - timedelta(minutes=2),
        "next_at": NOW + timedelta(minutes=8),
    }
    return poll_service.Check(**(fields | overrides))


INBOX_LAST = NOW - timedelta(minutes=34)
INBOX_NEXT = NOW + timedelta(minutes=26)


def poll(*items: poll_service.Check, background_running: bool = True) -> poll_service.PollStatus:
    checks = items or (
        check(),
        check(
            key=poll_service.LINKEDIN_INBOX,
            group=poll_service.CheckGroup.LINKEDIN,
            label="LinkedIn inbox",
            interval=timedelta(minutes=60),
            last_at=INBOX_LAST,
            next_at=INBOX_NEXT,
        ),
    )
    return poll_service.PollStatus(
        background_running=background_running, checks=checks, mailboxes=()
    )


def mailbox(**overrides: Any) -> MailboxRead:
    fields: dict[str, Any] = {
        "email": "sender@example.com",
        "status": MailboxStatus.OK,
        "arm": None,
        "status_reason": None,
        "updated_at": NOW - timedelta(days=30),
    }
    return MailboxRead(**(fields | overrides))


def attention(
    *,
    dashboard: str | None = None,
    gmail: str | None = None,
    linkedin: str | None = None,
    settings: str | None = None,
    stopped: str | None = None,
) -> attention_service.Attention:
    state = attention_service.ActivityState
    return attention_service.Attention(
        counts=attention_service.Counts(0, 0, 0, 0, 0, 0),
        flags=attention_service.Flags(dashboard, gmail, linkedin, settings),
        activity=attention_service.Activity(
            state.IDLE if stopped is None else state.STOPPED, stopped or "", None, None
        ),
    )


def run(**overrides: Any) -> RunRead:
    fields: dict[str, Any] = {
        "id": 9,
        "kind": SyncRunKind.ENRICH,
        "trigger": SyncRunTrigger.MANUAL,
        "started_at": NOW - timedelta(minutes=3),
    }
    return RunRead(**(fields | overrides))


def reads(**overrides: Any) -> Reads:
    fields: dict[str, Any] = {
        "mailboxes": [mailbox(arm=MailboxArm.SEND)],
        "poll": poll(),
        "attention": attention(),
        "linkedin": LinkedInRead(session_flag=None),
        "schedule": ScheduleRead(armed=True, paused=False, scheduler_running=True),
        "prefills": PrefillsRead(limit=15, remaining=12),
        "heat": HeatRead(tripped=False),
    }
    return Reads(**(fields | overrides))


def face(chip: Chip) -> dict[str, str]:
    """The chip as it reads, its tone, and where its button goes."""
    fact = "" if chip.fact == "" else f" · {chip.fact}"
    href = chip.target.to + ("" if chip.target.hash is None else f"#{chip.target.hash}")
    return {"tone": chip.tone, "text": f"{chip.name} · {chip.word}{fact}", "href": href}


def rows(chip: Chip) -> list[tuple[str, str, str]]:
    return [(part.label, part.tone, part.detail) for part in chip.parts]


def keys(chip: Chip) -> list[str]:
    return [part.key for part in chip.parts]


def find(chip: Chip, key: str) -> Part:
    return next(part for part in chip.parts if part.key == key)


def gmail(**overrides: Any) -> Chip:
    return service.gmail_chip(reads(**overrides))


def linkedin(**overrides: Any) -> Chip:
    return service.linkedin_chip(reads(**overrides))


# --- the scale --------------------------------------------------------------------------


def _part(key: str, tone: service.Tone) -> Part:
    return Part(key=key, label=key, tone=tone, word=key, detail=key)


def test_the_scale_ranks_off_above_ok_and_stopped_worst() -> None:
    assert dict(service.TONE_RANK) == {"ok": 0, "off": 1, "busy": 2, "warn": 3, "bad": 4}


def test_the_worst_part_leads_then_lead_order_then_the_list() -> None:
    parts = [_part("a", "ok"), _part("b", "warn"), _part("c", "warn"), _part("d", "busy")]
    assert service.lead_part(parts) == parts[1]
    assert service.lead_part(parts, ["c", "b"]) == parts[2]
    assert service.lead_part([_part("a", "ok"), _part("b", "ok")], ["b"]) == _part("b", "ok")
    assert service.lead_part([]) is None


def test_a_check_that_waits_on_its_own_reads_working_and_one_serve_doesnt_run_off() -> None:
    assert {state.value: tone for state, tone in service.CHECK_TONE.items()} == {
        "scheduled": "ok",
        "due": "ok",
        "idle": "ok",
        "outside_hours": "ok",
        "paused": "warn",
        "blocked": "warn",
        "off": "off",
        "not_running": "off",
        "not_wired": "off",
    }


def test_a_chip_with_no_parts_reads_unknown_and_off() -> None:
    chip = service.roll_up("gmail", "Gmail", [], lead=(), fact="", target=service.Target("/gmail"))
    assert face(chip) == {"tone": "off", "text": "Gmail · Unknown", "href": "/gmail"}


# --- Gmail ------------------------------------------------------------------------------


def test_gmail_says_how_it_sends_and_when_replies_were_last_checked() -> None:
    chip = gmail()
    assert face(chip) == {
        "tone": "ok",
        "text": "Gmail · Sending · checked {replies_last}",
        "href": "/gmail",
    }
    assert rows(chip) == [
        ("Mailbox", "ok", "sender@example.com · connected"),
        ("Sending", "ok", "Armed to send"),
        ("Reply check", "ok", "Checked {replies_last} · next {replies_next}"),
    ]
    assert chip.page == "Gmail"
    # Absolute times, each with how a client writes it.
    assert chip.times == {
        "replies_last": Time(NOW - timedelta(minutes=2), TimeStyle.AGO),
        "replies_next": Time(NOW + timedelta(minutes=8), TimeStyle.NEXT),
    }


def test_gmail_says_drafts_only_and_warns_when_not_armed() -> None:
    drafts = gmail(mailboxes=[mailbox(arm=MailboxArm.DRAFT)])
    assert face(drafts)["text"] == "Gmail · Drafts only · checked {replies_last}"
    assert face(gmail(mailboxes=[mailbox()])) == {
        "tone": "warn",
        "text": "Gmail · Not armed · checked {replies_last}",
        "href": "/settings#gmail",
    }


def test_gmail_stops_and_sends_you_to_settings_when_google_signed_it_out() -> None:
    chip = gmail(
        mailboxes=[mailbox(status=MailboxStatus.REAUTH_REQUIRED, status_reason="invalid_grant")]
    )
    assert face(chip) == {
        "tone": "bad",
        "text": "Gmail · Re-authorize · nothing sent or checked",
        "href": "/settings#gmail",
    }
    assert chip.page == "Settings"
    detail = chip.parts[0].detail
    assert detail.startswith("Google signed netkeep out of sender@example.com.")
    assert "revoked, or the consent screen is still in Testing" in detail
    # Nothing to say about arming a mailbox that can't be used.
    assert keys(chip) == ["mailbox", "replies"]


def test_an_unknown_reason_is_quoted() -> None:
    assert service.reason_text("weird_thing") == 'Google answered "weird_thing".'
    assert service.reason_text(None) is None
    assert service.reason_text("") is None


def test_gmail_reads_grey_when_no_mailbox_is_connected_or_it_was_disconnected() -> None:
    off = check(state=State.OFF, last_at=None, next_at=None)
    none = gmail(mailboxes=[], poll=poll(off))
    assert face(none) == {"tone": "off", "text": "Gmail · Not set up", "href": "/settings#gmail"}
    gone = gmail(mailboxes=[mailbox(status=MailboxStatus.DISABLED)])
    assert face(gone)["tone"] == "off"
    assert face(gone)["text"] == "Gmail · Disconnected"


def test_gmail_reads_the_live_mailbox_not_a_disabled_one() -> None:
    live = mailbox(email="live@example.com")
    assert service.pick_mailbox([mailbox(status=MailboxStatus.DISABLED), live]) == live
    newer = mailbox(status=MailboxStatus.DISABLED, updated_at=NOW - timedelta(days=1))
    older = mailbox(status=MailboxStatus.DISABLED, updated_at=NOW - timedelta(days=9))
    assert service.pick_mailbox([older, newer]) == newer
    assert service.pick_mailbox([]) is None


def test_gmail_spins_while_a_campaign_send_batch_runs() -> None:
    chip = gmail(sending=True)
    assert face(chip) == {
        "tone": "busy",
        "text": "Gmail · Sending now · campaign batch",
        "href": "/gmail",
    }
    assert find(chip, "batch").tone == "busy"


def test_gmail_says_why_the_reply_check_has_no_next_time() -> None:
    paused = check(state=State.PAUSED, next_at=None, reason="Sending hours are over")
    chip = gmail(poll=poll(paused))
    assert face(chip)["text"] == "Gmail · Paused · checked {replies_last}"
    assert chip.parts[-1].detail == "Checked {replies_last} · paused. Sending hours are over"
    assert "replies_next" not in chip.times
    never = gmail(poll=poll(check(last_at=None)))
    assert face(never)["text"] == "Gmail · Sending · not checked yet"
    assert find(never, "replies").detail == "Next {replies_next}"


def test_gmail_says_it_couldnt_check_instead_of_guessing() -> None:
    assert face(gmail(poll=FAILED))["text"] == "Gmail · Couldn’t check"
    chip = gmail(mailboxes=FAILED)
    assert face(chip) == {"tone": "off", "text": "Gmail · Couldn’t check", "href": "/gmail"}
    assert chip.parts[0].detail == "Couldn’t be read; netkeep tries again shortly"


def test_gmail_shows_a_draft_held_for_a_contact_you_shouldnt_email() -> None:
    flag = attention_service.DRAFT_FLAGGED
    chip = gmail(attention=attention(gmail=flag))
    assert face(chip) == {
        "tone": "warn",
        "text": "Gmail · Draft held",
        "href": "/gmail#leftover-drafts",
    }
    assert find(chip, "flag").detail == flag
    # A sign-in flag the mailbox part already says adds no row.
    reauth = gmail(
        mailboxes=[mailbox(status=MailboxStatus.REAUTH_REQUIRED)],
        attention=attention(gmail=attention_service.REAUTH_REQUIRED),
    )
    assert keys(reauth) == ["mailbox", "replies"]
    # One the list hasn't caught up with yet says so, and sends you to Settings.
    ahead = gmail(attention=attention(gmail=attention_service.REAUTH_REQUIRED))
    assert face(ahead) == {"tone": "warn", "text": "Gmail · Needs you", "href": "/settings#gmail"}


# --- LinkedIn ---------------------------------------------------------------------------


def test_linkedin_says_scheduled_runs_are_armed_and_when_the_inbox_was_checked() -> None:
    chip = linkedin()
    assert face(chip) == {
        "tone": "ok",
        "text": "LinkedIn · Armed · inbox {inbox_last}",
        "href": "/linkedin#schedule",
    }
    # No flag is not a healthy session: nothing here checks the live one.
    assert rows(chip) == [
        ("Session", "ok", "No session flag: no run has found LinkedIn logged out"),
        ("Scheduled runs", "ok", "Armed"),
        ("Inbox check", "ok", "Checked {inbox_last} · next {inbox_next}"),
        ("Prefills today", "ok", "12 of 15 left"),
        ("Heat", "ok", "Normal"),
    ]
    assert chip.times == {
        "inbox_last": Time(INBOX_LAST, TimeStyle.AGO),
        "inbox_next": Time(INBOX_NEXT, TimeStyle.NEXT),
    }
    never = linkedin(
        poll=poll(check(key=poll_service.LINKEDIN_INBOX, last_at=None, state=State.IDLE))
    )
    assert face(never)["text"] == "LinkedIn · Armed · inbox not checked yet"


def test_linkedin_names_the_next_scheduled_run() -> None:
    due = NOW + timedelta(minutes=20)
    chip = linkedin(
        schedule=ScheduleRead(armed=True, paused=False, scheduler_running=True, next_due=due)
    )
    assert chip.parts[1].detail == "Armed · next run {schedule_next}"
    assert chip.times["schedule_next"] == Time(due, TimeStyle.NEXT)


def test_the_soonest_stored_due_is_the_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_all`` takes the soonest of the served schedules' due times, not the first."""
    dues = iter([NOW + timedelta(hours=2), None, NOW + timedelta(minutes=20), None, None, None])
    monkeypatch.setattr(service, "stored_due", lambda *_args: next(dues, None))
    monkeypatch.setattr(service, "find_account", lambda *_args: None)
    monkeypatch.setattr(service, "account_id_for", lambda *_args: 1)
    found = service.read_all(
        None,  # type: ignore[arg-type]  # nothing here reaches the session
        None,  # type: ignore[arg-type]
        now=NOW,
        settings=_settings(),
        process=_process(),
    )
    assert isinstance(found.schedule, ScheduleRead)
    assert found.schedule.next_due == NOW + timedelta(minutes=20)


def _settings() -> Any:
    from netkeep.config import Settings

    return Settings()


def _process(**overrides: Any) -> service.Process:
    fields: dict[str, Any] = {
        "serving": poll_service.Serving(),
        "scheduler_running": True,
        "key_persisted": True,
    }
    return service.Process(**(fields | overrides))


def test_linkedin_spins_with_the_running_runs_progress() -> None:
    chip = linkedin(linkedin=LinkedInRead(None, run(planned=626, completed=340)))
    assert face(chip) == {
        "tone": "busy",
        "text": "LinkedIn · Enrichment running · 340 of 626",
        "href": "/linkedin#runs",
    }
    assert chip.parts[1].label == "Running"
    assert chip.parts[1].detail == "Enrichment · 340 of 626 · started {run_started}"
    assert chip.times["run_started"] == Time(NOW - timedelta(minutes=3), TimeStyle.AGO)
    sync = linkedin(linkedin=LinkedInRead(None, run(kind=SyncRunKind.CONNECTIONS_INCREMENTAL)))
    assert face(sync)["text"] == "LinkedIn · Sync running · started {run_started}"
    auto = run(kind=SyncRunKind.MESSAGE_SEND, trigger=SyncRunTrigger.SCHEDULED)
    chip = linkedin(linkedin=LinkedInRead(None, auto))
    assert face(chip)["text"] == "LinkedIn · Auto-send running · started {run_started}"
    assert chip.parts[1].detail == "LinkedIn auto-send · started {run_started}"


def test_the_running_count_moves_with_each_progress_event_and_reads_a_syncs_count() -> None:
    sync = run(
        kind=SyncRunKind.CONNECTIONS_FULL,
        progress={"mode": "full", "pages": 4, "connections": 120, "total": 626},
    )
    status = LinkedInRead(None, sync)
    assert face(linkedin(linkedin=status))["text"] == "LinkedIn · Full sync running · 120 of 626"
    live = {"run_id": 9, "mode": "full", "pages": 9, "connections": 340, "total": 626}
    assert (
        face(linkedin(linkedin=status, progress=live))["text"]
        == "LinkedIn · Full sync running · 340 of 626"
    )
    # Another run's event is not this one's.
    other = live | {"run_id": 8}
    assert (
        face(linkedin(linkedin=status, progress=other))["text"]
        == "LinkedIn · Full sync running · 120 of 626"
    )


def test_only_the_progress_shapes_it_knows_count() -> None:
    assert service.progress_text({"planned": 50, "visited": 12, "harvested": 10}) == "12 of 50"
    assert service.progress_text({"connections": 340, "total": 626}) == "340 of 626"
    assert service.progress_text({"connections": 340, "total": None}) is None
    assert service.progress_text({"planned": 0, "visited": 0}) is None
    assert service.progress_text({"visited": "3", "planned": 5}) is None
    assert service.progress_text({"visited": True, "planned": 5}) is None
    assert service.progress_text({"visited": float("nan"), "planned": 5}) is None
    assert service.progress_text(None) is None


def test_a_logged_out_session_or_a_checkpoint_stops_above_everything_else() -> None:
    out = linkedin(
        linkedin=LinkedInRead("logged_out", run()),
        schedule=ScheduleRead(armed=True, paused=True, scheduler_running=True),
    )
    assert face(out) == {
        "tone": "bad",
        "text": "LinkedIn · Logged out · runs stopped",
        "href": "/linkedin#session",
    }
    assert out.page == "LinkedIn"
    checkpoint = linkedin(linkedin=LinkedInRead("checkpoint"))
    assert face(checkpoint)["text"] == "LinkedIn · Checkpoint · runs stopped"


@pytest.mark.parametrize(
    ("schedule", "tone", "text"),
    [
        (
            ScheduleRead(armed=True, paused=True, scheduler_running=True),
            "warn",
            "LinkedIn · Paused · inbox {inbox_last}",
        ),
        (ScheduleRead(armed=False, paused=False, scheduler_running=True), "off", "LinkedIn · Off"),
        (
            ScheduleRead(armed=False, paused=True, scheduler_running=True),
            "warn",
            "LinkedIn · Paused · inbox {inbox_last}",
        ),
        (
            ScheduleRead(armed=True, paused=False, scheduler_running=False),
            "bad",
            "LinkedIn · No scheduler · runs don’t start",
        ),
    ],
)
def test_linkedin_says_whether_scheduled_runs_are_paused_off_or_have_no_scheduler(
    schedule: ScheduleRead, tone: str, text: str
) -> None:
    chip = linkedin(schedule=schedule)
    assert face(chip) == {"tone": tone, "text": text, "href": "/linkedin#schedule"}


def test_linkedin_warns_once_todays_prefills_are_spent() -> None:
    chip = linkedin(prefills=PrefillsRead(limit=15, remaining=0))
    assert face(chip) == {
        "tone": "warn",
        "text": "LinkedIn · No prefills left · until tomorrow",
        "href": "/linkedin#budget",
    }
    # Overspent reads as none left, never a negative count.
    assert find(linkedin(prefills=PrefillsRead(15, -2)), "prefills").detail == "0 of 15 left"
    # No li_prefills row: no part, rather than a guess.
    assert "prefills" not in keys(linkedin(prefills=None))


def test_linkedin_stops_for_heat_over_its_threshold_saying_when_runs_resume() -> None:
    resumes = datetime(2099, 1, 2, 15, 40, tzinfo=UTC)
    chip = linkedin(heat=HeatRead(tripped=True, resumes_at=resumes))
    assert face(chip) == {
        "tone": "bad",
        "text": "LinkedIn · Heat high · runs wait until {heat_resumes}",
        "href": "/linkedin#heat",
    }
    assert chip.times["heat_resumes"] == Time(resumes, TimeStyle.WHEN)
    unknown = linkedin(heat=HeatRead(tripped=True))
    assert face(unknown)["text"] == "LinkedIn · Heat high · runs wait"
    assert find(unknown, "heat").detail == (
        "Over its threshold: scheduled runs are skipped until it cools"
    )


def test_linkedin_warns_while_auto_send_is_held() -> None:
    chip = linkedin(attention=attention(dashboard=attention_service.AUTO_SEND_HELD))
    assert face(chip) == {
        "tone": "warn",
        "text": "LinkedIn · Auto-send held",
        "href": "/#needs-you",
    }
    assert chip.page == "Dashboard"
    assert chip.parts[-1].detail == attention_service.AUTO_SEND_HELD


def test_linkedin_stops_for_a_tripped_breaker() -> None:
    breaker = attention_service.BREAKER_TRIPPED
    chip = linkedin(attention=attention(settings=breaker, stopped=breaker))
    assert face(chip) == {
        "tone": "bad",
        "text": "LinkedIn · Breaker tripped · some runs can’t start",
        "href": "/settings#posture",
    }
    assert chip.page == "Settings"
    assert "blocked" not in keys(chip)


def test_a_tripped_breaker_never_reads_green() -> None:
    """The safety pin of #751: whatever else is well, a breaker reads red."""
    breaker = attention_service.BREAKER_TRIPPED
    chip = linkedin(attention=attention(settings=breaker))
    assert chip.tone == "bad"


def test_linkedin_never_reads_green_while_attention_says_it_is_blocked() -> None:
    label = attention_service.SESSION_FLAGGED
    chip = linkedin(attention=attention(linkedin=label, stopped=label))
    assert face(chip)["tone"] == "warn"
    assert face(chip)["text"] == "LinkedIn · Blocked"
    assert (chip.parts[-1].label, chip.parts[-1].detail) == ("Blocked", label)
    # Once a part explains it, that part speaks and there is no extra row.
    caught = linkedin(
        linkedin=LinkedInRead("logged_out"), attention=attention(linkedin=label, stopped=label)
    )
    assert face(caught)["text"] == "LinkedIn · Logged out · runs stopped"
    assert "blocked" not in keys(caught)
    # Without a label it still says something.
    nameless = attention(stopped="")
    nameless = replace(
        nameless,
        activity=replace(nameless.activity, state=attention_service.ActivityState.STOPPED),
    )
    assert find(linkedin(attention=nameless), "blocked").detail == (
        "Something blocks LinkedIn runs from starting"
    )


def test_linkedin_says_it_couldnt_check_instead_of_guessing() -> None:
    chip = linkedin(linkedin=FAILED, schedule=FAILED, prefills=FAILED)
    assert face(chip) == {
        "tone": "off",
        "text": "LinkedIn · Couldn’t check",
        "href": "/linkedin#session",
    }
    assert ("Prefills today", "off", "Couldn’t be read; netkeep tries again shortly") in rows(chip)
    assert find(linkedin(heat=FAILED), "heat").word == "Couldn’t check"
    assert find(linkedin(poll=FAILED), "inbox").target == service.Target("/linkedin", "inbox")


# --- netkeep ----------------------------------------------------------------------------


def _netkeep(**overrides: Any) -> Chip:
    return service.netkeep_chip(reads(**overrides))


def test_netkeep_says_serve_runs_the_background_checks() -> None:
    chip = _netkeep()
    assert rows(chip) == [("Background checks", "ok", "netkeep serve is running them")]
    assert chip.target == service.SUPPORT
    assert chip.page == "Settings › Support"


def test_netkeep_warns_when_serve_isnt_running_the_background_checks() -> None:
    chip = _netkeep(poll=poll(background_running=False))
    assert face(chip)["tone"] == "warn"
    assert face(chip)["text"] == "netkeep · No background checks · serve isn’t running"


def test_netkeep_never_reads_green_when_the_background_checks_cant_be_read() -> None:
    chip = _netkeep(poll=FAILED)
    assert face(chip)["tone"] == "off"
    assert face(chip)["text"] == "netkeep · Couldn’t check"
    assert find(chip, "checks").label == "Background checks"


def test_netkeep_warns_when_the_keychain_refused_the_key_before_the_checks() -> None:
    chip = _netkeep(key_persisted=False, poll=poll(background_running=False))
    assert face(chip)["text"] == "netkeep · Keychain refused · key in memory"
    assert chip.parts[-1] == Part(
        key="keychain",
        label="Keychain",
        tone="warn",
        word="Keychain refused",
        detail="Refused · the local API key lives in memory until netkeep stops",
        fact="key in memory",
    )


def test_a_client_rolls_netkeep_up_again_with_its_own_parts() -> None:
    """The menu bar's way (ADR 0014 rule 1): its "server" part, the same lead and calm."""
    server = Part("server", "Server", "ok", "Connected", "Reachable · 0.1.0")
    chip = _netkeep()
    again = service.roll_up(
        "netkeep",
        "netkeep",
        [server, *chip.parts],
        lead=service.NETKEEP_LEAD,
        calm=service.NETKEEP_CALM,
        fact="",
        target=service.SUPPORT,
    )
    assert face(again)["text"] == "netkeep · Connected"
    down = replace(server, tone="bad", word="Unreachable", fact="retrying")
    worse = service.roll_up(
        "netkeep",
        "netkeep",
        [down, *_netkeep(key_persisted=False).parts],
        lead=service.NETKEEP_LEAD,
        calm=service.NETKEEP_CALM,
        fact="",
        target=service.SUPPORT,
    )
    assert face(worse)["text"] == "netkeep · Unreachable · retrying"


# --- what the menu bar needs besides ----------------------------------------------------


def test_the_paused_run_and_sending_fields() -> None:
    quiet = service.system_status(reads())
    assert (quiet.linkedin_paused, quiet.linkedin_run, quiet.campaign_sending) == (
        False,
        None,
        False,
    )
    auto = run(kind=SyncRunKind.MESSAGE_SEND, trigger=SyncRunTrigger.SCHEDULED)
    busy = service.system_status(
        reads(
            linkedin=LinkedInRead(None, auto),
            schedule=ScheduleRead(armed=True, paused=True, scheduler_running=True),
            campaign_busy=True,
        )
    )
    assert busy.linkedin_paused is True
    assert busy.linkedin_run == service.RunInProgress(
        id=9, kind=SyncRunKind.MESSAGE_SEND, label="LinkedIn auto-send", started_at=auto.started_at
    )
    assert busy.campaign_sending is True
    # A schedule that couldn't be read is not "paused": the menu offers Pause.
    assert service.system_status(reads(schedule=FAILED)).linkedin_paused is False


def test_a_send_batch_shows_for_fifteen_seconds_after_the_tick_that_sent() -> None:
    assert timedelta(seconds=15) == service.BATCH_SHOWS_FOR

    def sending(ago: timedelta) -> bool:
        found = service.read_all(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            now=NOW,
            settings=_settings(),
            process=_process(last_sent_at=NOW - ago),
        )
        return found.sending

    assert sending(timedelta(seconds=14)) is True
    assert sending(timedelta(seconds=15)) is False


def test_a_read_that_raises_fails_only_its_own_part(caplog: pytest.LogCaptureFixture) -> None:
    """``read_all`` with no session: every database read raises, and says so, by type only."""
    found = service.read_all(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        now=NOW,
        settings=_settings(),
        process=_process(key_persisted=False),
    )
    assert found.mailboxes is FAILED and found.poll is FAILED and found.heat is FAILED
    assert found.key_persisted is False
    status = service.system_status(found)
    assert status.gmail.tone == "off" and status.linkedin.tone == "off"
    assert "AttributeError" in caplog.text


def test_attention_that_cant_be_read_never_reads_green() -> None:
    """Without ``/attention``'s flags a breaker, a hold or a block can't be known: a part
    says so, so the chip is never green for want of one."""
    chip = linkedin(attention=FAILED)
    assert chip.tone != "ok"
    assert face(chip) == {
        "tone": "off",
        "text": "LinkedIn · Couldn’t check",
        "href": "/settings#posture",
    }
    assert find(chip, "breaker").label == "Breakers and holds"
    assert "held" not in keys(chip) and "blocked" not in keys(chip)
    # A stopped session still speaks over it.
    assert face(linkedin(attention=FAILED, linkedin=LinkedInRead("checkpoint")))["tone"] == "bad"
    gmail_chip = gmail(attention=FAILED)
    assert gmail_chip.tone != "ok"
    assert (find(gmail_chip, "flag").label, find(gmail_chip, "flag").tone) == ("Needs you", "off")


def _ts_reasons() -> dict[str, str]:
    """``REASONS`` in ``frontend/src/features/mailboxes/api.ts``, read from the source."""
    source = (
        Path(__file__).resolve().parents[1] / "frontend/src/features/mailboxes/api.ts"
    ).read_text(encoding="utf-8")
    table = source[source.index("const REASONS: Record<string, string> = {") :]
    table = table[: table.index("\n}\n")]
    entries = re.findall(r"^\s+(\w+):\s*'((?:[^'\\]|\\.)*)',?$", table, flags=re.MULTILINE)
    return dict(entries)


def test_the_reasons_match_the_uis_word_for_word() -> None:
    """The server's copy of a mailbox reason reads as the Gmail settings page says it."""
    ui = _ts_reasons()
    assert len(ui) >= len(service.REASONS) > 0
    for reason, text in service.REASONS.items():
        assert ui.get(reason) == text, reason
