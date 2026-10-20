"""
Morning and evening fatigue tracking, and the 9am reminder that keeps the
morning reading from being lost to the day.

Fatigue is logged as two 0-10 readings a day (blank = not logged, so 0
stays a real answer). These tests pin the whole path: schema, storage and
merge, the worst-reading rule the model scores by, both entry forms, and
the reminder's send/skip/rate-limit behavior.
"""

import sqlite3
from datetime import datetime

import pytest

import db
import flaremodel
import reminders


@pytest.fixture
def user(fresh_db):
    return db.create_user("patient", "Test Patient", "not-a-real-password-hash")


def log_in(app_client, user_id):
    with app_client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def today_str():
    # Mirror _check_fatigue_reminder's own clock: user timezone with a
    # naive-local fallback, so the test agrees with the code on the date.
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Chicago"))
    except Exception:
        now = datetime.now()
    return now.strftime("%Y-%m-%d")


# ------------------------------------------------------------------
# Schema
# ------------------------------------------------------------------

def test_schema_has_morning_and_evening_columns(fresh_db):
    con = sqlite3.connect(fresh_db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(daily_observations)")}
    finally:
        con.close()
    assert {"fatigue_morning", "fatigue_evening", "stressors"} <= cols


# ------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------

def test_morning_and_evening_round_trip(user):
    db.upsert_daily_observations(user, {"date": "2026-10-20",
                                        "fatigue_morning": 6.5,
                                        "fatigue_evening": 8.0})
    row = db.get_daily_observations(user, "2026-10-20")
    assert row["fatigue_morning"] == 6.5
    assert row["fatigue_evening"] == 8.0


def test_a_later_partial_update_does_not_wipe_the_morning_reading(user):
    # The mobile quick log merges: only fields with values are submitted.
    db.upsert_daily_observations(user, {"date": "2026-10-20", "fatigue_morning": 6.5})
    db.upsert_daily_observations(user, {"date": "2026-10-20", "fatigue_evening": 7.0})
    row = db.get_daily_observations(user, "2026-10-20")
    assert row["fatigue_morning"] == 6.5
    assert row["fatigue_evening"] == 7.0


# ------------------------------------------------------------------
# The worst-reading rule
# ------------------------------------------------------------------

def test_effective_fatigue_takes_the_worst_reading_logged():
    assert flaremodel.effective_fatigue(
        {"fatigue_scale": 2, "fatigue_morning": 6, "fatigue_evening": 3}) == 6
    assert flaremodel.effective_fatigue(
        {"fatigue_morning": 6, "fatigue_evening": 3}) == 6
    assert flaremodel.effective_fatigue({"fatigue_morning": 0}) == 0  # 0 is a real reading
    assert flaremodel.effective_fatigue({"fatigue_scale": 7}) == 7    # legacy rows
    assert flaremodel.effective_fatigue({}) is None


def test_scoring_uses_the_worst_of_morning_and_evening(user):
    items = flaremodel._score_items({"date": "2026-10-20",
                                     "fatigue_morning": 6, "fatigue_evening": 3},
                                    user_id=user)
    fatigue_items = [i for i in items if "fatigue" in i["name"]]
    assert fatigue_items, "expected a fatigue contribution at level 6"
    assert fatigue_items[0]["points"] == pytest.approx(2.5)


def test_scoring_ignores_fatigue_when_nothing_was_logged(user):
    items = flaremodel._score_items({"date": "2026-10-20"}, user_id=user)
    assert not [i for i in items if "fatigue" in i["name"]]


# ------------------------------------------------------------------
# Forms
# ------------------------------------------------------------------

def test_daily_form_stores_both_readings(app_client, user):
    log_in(app_client, user)
    resp = app_client.post("/daily", data={
        "date": "2026-10-20", "fatigue_morning": "6.5", "fatigue_evening": "8",
        "pain_scale": "0", "fatigue_scale": "0", "emotional_state": "5",
    }, follow_redirects=True)
    assert resp.status_code == 200
    row = db.get_daily_observations(user, "2026-10-20")
    assert row["fatigue_morning"] == 6.5
    assert row["fatigue_evening"] == 8.0


def test_a_blank_reading_stores_as_not_logged(app_client, user):
    log_in(app_client, user)
    app_client.post("/daily", data={
        "date": "2026-10-20", "fatigue_morning": "6.5",
        "pain_scale": "0", "fatigue_scale": "0", "emotional_state": "5",
    }, follow_redirects=True)
    row = db.get_daily_observations(user, "2026-10-20")
    assert row["fatigue_morning"] == 6.5
    assert row["fatigue_evening"] is None


def test_daily_form_renders_the_new_fields(app_client, user, monkeypatch):
    log_in(app_client, user)
    # The page normally smart-fetches UV from a weather API; stub it so the
    # test suite's no-network rule holds and the assertion is about the form.
    import uv_fetcher
    monkeypatch.setattr(uv_fetcher, "smart_fetch_uv_for_date",
                        lambda *a, **k: {"uv_morning": 0, "uv_noon": 0,
                                         "uv_evening": 0, "source": "manual",
                                         "forecast": None})
    html = app_client.get("/daily?date=2026-10-19").get_data(as_text=True)
    assert 'name="fatigue_morning"' in html
    assert 'name="fatigue_evening"' in html
    # Quick-note triggers (+++ dropdown, hamburger) and the modal ship on
    # every page via base.html.
    assert 'id="quickNoteBtnP"' in html
    assert 'id="quickNoteBtnH"' in html
    assert 'id="quickNoteModal"' in html


def test_mobile_quick_log_merges_both_readings(app_client, user):
    log_in(app_client, user)
    app_client.post("/mobile/log", data={"fatigue_morning": "6"})
    app_client.post("/mobile/log", data={"fatigue_evening": "7"})
    row = db.get_daily_observations(user, today_str())
    assert row["fatigue_morning"] == 6
    assert row["fatigue_evening"] == 7


# ------------------------------------------------------------------
# The 9am reminder
# ------------------------------------------------------------------

def test_the_scheduler_registers_the_9am_fatigue_job():
    jobs = [j.id for j in reminders._scheduler.get_jobs()]
    assert "fatigue_reminder" in jobs


def test_morning_reminder_fires_for_a_user_with_ntfy(user, monkeypatch):
    db.upsert_user_preferences(user, {"ntfy_topic": "test-topic"})
    sent = []

    def fake(msg, title=None, priority=None, tags=None, server=None, topic=None):
        sent.append((title, topic))

    monkeypatch.setattr(reminders, "_send_ntfy_alert", fake)

    reminders._check_fatigue_reminder()

    assert len(sent) == 1
    assert sent[0][0] == "Morning fatigue check-in"
    assert sent[0][1] == "test-topic"


def test_no_reminder_once_the_morning_reading_is_logged(user, monkeypatch):
    db.upsert_user_preferences(user, {"ntfy_topic": "test-topic"})
    db.upsert_daily_observations(user, {"date": today_str(), "fatigue_morning": 5})
    sent = []
    monkeypatch.setattr(reminders, "_send_ntfy_alert",
                        lambda *a, **k: sent.append(a))

    reminders._check_fatigue_reminder()
    assert sent == []


def test_reminder_is_rate_limited_to_one_per_day(user, monkeypatch):
    db.upsert_user_preferences(user, {"ntfy_topic": "test-topic"})
    sent = []
    monkeypatch.setattr(reminders, "_send_ntfy_alert",
                        lambda *a, **k: sent.append(a))

    reminders._check_fatigue_reminder()
    reminders._check_fatigue_reminder()

    assert len(sent) == 1
    prefs = db.get_user_preferences(user)
    assert prefs.get("last_fatigue_reminder_date") == today_str()


# ------------------------------------------------------------------
# Quick note button (nav, between today and forecast)
# ------------------------------------------------------------------

def test_note_button_appends_a_timestamped_entry(app_client, user):
    log_in(app_client, user)
    resp = app_client.post("/note/quick", json={"date": "2026-10-20", "time": "14:32",
                                                "text": "felt dizzy standing up"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    row = db.get_daily_observations(user, "2026-10-20")
    assert row["notes"] == "[14:32] felt dizzy standing up"


def test_note_button_appends_to_a_day_that_already_has_notes(app_client, user):
    log_in(app_client, user)
    db.upsert_daily_observations(user, {"date": "2026-10-20",
                                        "notes": "[09:00] rough morning",
                                        "fatigue_morning": 6})
    resp = app_client.post("/note/quick", json={"date": "2026-10-20", "time": "15:10",
                                                "text": "nap helped"})
    assert resp.status_code == 200
    row = db.get_daily_observations(user, "2026-10-20")
    assert row["notes"] == "[09:00] rough morning\n[15:10] nap helped"
    assert row["fatigue_morning"] == 6  # append merges; other fields untouched


def test_note_button_rejects_empty_and_malformed_input(app_client, user):
    log_in(app_client, user)
    assert app_client.post("/note/quick", json={"date": "2026-10-20", "time": "14:32",
                                                "text": "   "}).status_code == 400
    assert app_client.post("/note/quick", json={"date": "not-a-date", "time": "14:32",
                                                "text": "x"}).status_code == 400
    assert app_client.post("/note/quick", json={"date": "2026-10-20", "time": "99:99",
                                                "text": "x"}).status_code == 400


# ------------------------------------------------------------------
# Stressors log (replaces 'uv lag' in the nav)
# ------------------------------------------------------------------

def test_stressors_log_appends_and_displays_newest_first(app_client, user):
    log_in(app_client, user)
    app_client.post("/stress/quick", data={"date": "2026-10-19", "time": "08:00",
                                           "text": "court paperwork"})
    app_client.post("/stress/quick", data={"date": "2026-10-20", "time": "17:45",
                                           "text": "argument with neighbor"})
    row = db.get_daily_observations(user, "2026-10-20")
    assert row["stressors"] == "[17:45] argument with neighbor"

    html = app_client.get("/stress").get_data(as_text=True)
    assert "argument with neighbor" in html
    assert "court paperwork" in html
    assert html.index("argument with neighbor") < html.index("court paperwork")


def test_stressors_log_rejects_empty_entries(app_client, user):
    log_in(app_client, user)
    resp = app_client.post("/stress/quick", data={"date": "2026-10-20", "time": "08:00",
                                                  "text": ""}, follow_redirects=False)
    assert resp.status_code == 302
    assert "error" in resp.headers["Location"]
    assert db.get_daily_observations(user, "2026-10-20") is None


# ------------------------------------------------------------------
# Medication log (replaces 'reactions' in the nav)
# ------------------------------------------------------------------

def test_medication_log_schedules_consumption_times(app_client, user):
    log_in(app_client, user)
    med = db.add_medication(user, {"drug_name": "apixaban", "dose": 5, "unit": "mg",
                                   "start_date": "2026-09-01"})
    resp = app_client.post("/medlog/dose/add", data={"medication_id": str(med),
                                                    "date": "2026-10-20", "time": "09:00",
                                                    "repeat_days": "3"}, follow_redirects=True)
    assert resp.status_code == 200
    doses = db.get_todays_doses(user, "2026-10-20")
    assert len(doses) == 1
    assert doses[0]["scheduled_datetime"] == "2026-10-20 09:00"
    assert doses[0]["dose_label"] == "apixaban 5 mg"
    assert len(db.get_todays_doses(user, "2026-10-22")) == 1  # repeats daily
    html = app_client.get("/medlog").get_data(as_text=True)
    assert "consumption times" in html
    assert "09:00" in html


def test_reminder_control_toggles_dose_notifications(app_client, user):
    log_in(app_client, user)
    med = db.add_medication(user, {"drug_name": "apixaban", "dose": 5, "unit": "mg",
                                   "start_date": "2026-09-01"})
    taper = db.create_taper_schedule(user, med, "2026-10-01")
    db.insert_scheduled_doses(user, [{
        "taper_schedule_id": taper, "medication_id": med,
        "scheduled_datetime": "2026-10-20 09:00", "dose_label": "apixaban 5 mg",
        "dose_amount": 5, "dose_unit": "mg"}])
    db.upsert_user_preferences(user, {"ntfy_topic": "t"})

    window = ("2026-10-20 09:00", "2026-10-20 09:01")
    assert len(db.get_all_pending_doses_with_ntfy(*window)) == 1  # default: on

    app_client.post("/medlog/reminders", data={"enabled": "0"})
    assert db.get_user_preferences(user)["med_reminders_enabled"] == 0
    assert db.get_all_pending_doses_with_ntfy(*window) == []      # gated off

    app_client.post("/medlog/reminders", data={"enabled": "1"})
    assert len(db.get_all_pending_doses_with_ntfy(*window)) == 1  # back on


# ------------------------------------------------------------------
# Activity log + data dropdown nav
# ------------------------------------------------------------------

def test_activity_log_lists_everything_newest_first(app_client, user):
    log_in(app_client, user)
    app_client.post("/note/quick", json={"date": "2026-10-19", "time": "09:15",
                                        "text": "slept badly"})
    app_client.post("/stress/quick", data={"date": "2026-10-20", "time": "17:45",
                                          "text": "argument with neighbor"})
    app_client.post("/daily", data={"date": "2026-10-20", "fatigue_morning": "6",
                                    "fatigue_evening": "7", "pain_scale": "2",
                                    "emotional_state": "5"})
    html = app_client.get("/activity").get_data(as_text=True)
    assert "argument with neighbor" in html
    assert "slept badly" in html
    assert "fatigue am 6.0 / pm 7.0" in html
    assert "pain 2.0" in html
    assert html.index("argument with neighbor") < html.index("slept badly")  # newest day first


def test_nav_has_data_dropdown_with_add_note_and_activity_log(app_client, user):
    log_in(app_client, user)
    html = app_client.get("/stress").get_data(as_text=True)
    assert 'id="dataDropdown"' in html          # the dropdown itself
    assert ">add note" in html                   # renamed note button inside it
    assert 'href="/forecast"' in html             # forecast moved in
    assert 'href="/model"' in html               # model moved in
    assert 'href="/search"' in html              # search moved in
    assert "full log" in html                     # renamed view log
    assert 'id="plusDropdown"' in html            # +++ quick-log dropdown
    assert 'href="/stress"' in html and 'href="/medlog"' in html
    assert 'id="navHamburgerBtn"' in html        # narrow-screen hamburger
    assert 'id="headerControls"' in html          # movable right-side controls