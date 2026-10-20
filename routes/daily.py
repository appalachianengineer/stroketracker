"""
Home page, daily symptom entry, and the mobile quick log.
"""

from datetime import date, datetime, timedelta
from flask import jsonify, render_template, request, redirect, url_for
from flask_login import login_required
import db
import uv_fetcher

from appcore import CONFIG, app, get_location_key, uid
from flaremodel import VALID_FLARE_SEVERITIES, _compute_bbt_hint, _inject_cycle_phase, _inject_scoring_context, calculate_flare_prime_score, get_contributing_factors, get_current_weights, get_risk_level


# ============================================================
# Index
# ============================================================

@app.route("/")
def index():
    """Home page - redirects to daily entry for today."""
    return redirect(url_for("daily_entry"))


# ============================================================
# Daily entry
# ============================================================

@app.route("/daily", methods=["GET"])
def daily_entry():
    """Daily entry form with date navigation. Defaults to today."""
    # Get date from query param or use today
    date_param = request.args.get("date")
    if date_param:
        try:
            entry_date = datetime.strptime(date_param, "%Y-%m-%d").date()
        except ValueError:
            entry_date = date.today()
    else:
        entry_date = date.today()
    
    entry_date_str = entry_date.isoformat()
    
    # Calculate prev/next dates
    prev_date = (entry_date - timedelta(days=1)).isoformat()
    next_date = (entry_date + timedelta(days=1)).isoformat()
    is_today = (entry_date == date.today())
    
    # Smart UV fetch — handles today's unresolved zeros gracefully
    prefs = db.get_user_preferences(uid()) or {}
    tz = prefs.get("timezone") or CONFIG.get("timezone", "America/Chicago")
    uv = uv_fetcher.smart_fetch_uv_for_date(entry_date_str, get_location_key(), tz)
    forecast = uv.get("forecast") if uv else None

    # Load any existing entry for this date
    existing = db.get_daily_observations(uid(), entry_date_str)
    
    # Load active medications for the sidebar
    active_meds = db.get_active_medications(uid())

    # Load today's scheduled doses for the reminder checklist
    todays_doses = db.get_todays_doses(uid(), entry_date_str)

    quick_mode = request.args.get("mode") == "quick"

    # BBT baseline hint for cycle trackers
    bbt_hint = None
    if prefs.get('track_cycle', CONFIG.get('track_cycle')):
        try:
            bbt_hint = _compute_bbt_hint(uid())
        except Exception:
            pass

    # Recent HealthKit sync events for the trust panel at the top of /daily
    try:
        recent_syncs = db.get_recent_health_sync_events(uid(), limit=3)
    except Exception:
        recent_syncs = []

    return render_template(
        "daily_entry.html",
        entry_date=entry_date_str,
        existing=existing,
        uv=uv,
        forecast=forecast,
        active_meds=active_meds,
        todays_doses=todays_doses,
        prev_date=prev_date,
        next_date=next_date,
        is_today=is_today,
        quick_mode=quick_mode,
        bbt_hint=bbt_hint,
        recent_syncs=recent_syncs,
    )


def _clean_flare_severity(raw):
    """Return raw only if it's a recognized severity, else None."""
    raw = (raw or "").strip()
    return raw if raw in VALID_FLARE_SEVERITIES else None


@app.route("/daily", methods=["POST"])
def daily_entry_submit():
    """Handle daily entry form submission."""
    form = request.form

    def get_bool(key):
        return 1 if form.get(key) == "on" else 0

    def get_float(key, default=None):
        val = form.get(key, "").strip()
        try:
            return float(val) if val else default
        except ValueError:
            return default

    data = {
        "date": form.get("date", date.today().isoformat()),
        "steps": get_float("steps"),
        "hours_slept": get_float("hours_slept"),
        "hrv": get_float("hrv"),
        "hrv_rmssd": get_float("hrv_rmssd"),
        "resting_heart_rate": get_float("resting_heart_rate"),
        "spo2": get_float("spo2"),
        "respiratory_rate": get_float("respiratory_rate"),
        "basal_temp_delta": get_float("basal_temp_delta"),
        "sun_exposure_min": get_float("sun_exposure_min"),
        "pain_scale": get_float("pain_scale"),
        "fatigue_scale": get_float("fatigue_scale"),
        "fatigue_morning": get_float("fatigue_morning"),
        "fatigue_evening": get_float("fatigue_evening"),
        "emotional_state": get_float("emotional_state"),
        "emotional_notes": form.get("emotional_notes", "").strip() or None,
        "neurological": get_bool("neurological"),
        "neuro_notes": form.get("neuro_notes", "").strip() or None,
        "cognitive": get_bool("cognitive"),
        "cognitive_notes": form.get("cognitive_notes", "").strip() or None,
        "musculature": get_bool("musculature"),
        "musculature_notes": form.get("musculature_notes", "").strip() or None,
        "migraine": get_bool("migraine"),
        "migraine_notes": form.get("migraine_notes", "").strip() or None,
        "pulmonary": get_bool("pulmonary"),
        "pulmonary_notes": form.get("pulmonary_notes", "").strip() or None,
        "gastro": get_bool("gastro"),
        "gastro_notes": form.get("gastro_notes", "").strip() or None,
        "mucosal": get_bool("mucosal"),
        "mucosal_notes": form.get("mucosal_notes", "").strip() or None,
        "dermatological": get_bool("dermatological"),
        "derm_notes": form.get("derm_notes", "").strip() or None,
        "rheumatic": get_bool("rheumatic"),
        "rheumatic_notes": form.get("rheumatic_notes", "").strip() or None,
        "strike_physical": get_bool("strike_physical"),
        "strike_environmental": get_bool("strike_environmental"),
        "flare_occurred": get_bool("flare_occurred"),
        "flare_severity": _clean_flare_severity(form.get("flare_severity")) if form.get("flare_occurred") else None,
        "notes": form.get("notes", "").strip() or None,
        "period_flow": form.get("period_flow") or None,
        "cramping": form.get("cramping") or None,
        "cycle_notes": form.get("cycle_notes", "").strip() or None,
        "stayed_indoors": 1 if form.get("stayed_indoors") else 0,
        "uv_protection_level": form.get("uv_protection_level") or None,
    }

    # If stayed indoors, force consistent values
    if data["stayed_indoors"]:
        data["sun_exposure_min"] = 0
        data["uv_protection_level"] = "indoors_only"

    db.upsert_daily_observations(uid(), data)
    db.upsert_user_preferences(uid(), {"last_logged_at": datetime.now().isoformat()})
    return redirect(url_for("daily_confirm", entry_date=data["date"]))


@app.route("/uv/manual", methods=["POST"])
@login_required
def uv_manual():
    """Save manually entered UV values."""
    data = request.get_json(force=True)
    date_str = data.get("date", date.today().isoformat())
    uv_fetcher.store_manual_uv(
        date_str=date_str,
        uv_morning=float(data.get("uv_morning", 0)),
        uv_noon=float(data.get("uv_noon", 0)),
        uv_evening=float(data.get("uv_evening", 0)),
        location_key=get_location_key(),
    )
    return jsonify({"ok": True})


@app.route("/daily/confirm/<entry_date>")
def daily_confirm(entry_date):
    """Confirmation screen after daily entry submission."""
    entry = db.get_daily_observations(uid(), entry_date)
    uv = db.get_uv_data(get_location_key(), entry_date)
    return render_template("daily_confirm.html", entry=entry, uv=uv)


# ============================================================
# Quick note (nav button) + stressors log
# ============================================================

def _append_log_entry(user_id: int, date_str: str, time_str: str, text: str,
                      field: str) -> tuple:
    """Shared quick-log append: '[HH:MM] text' onto the chosen day's log
    field ('notes' or 'stressors'), preserving what is already there.
    Returns (error, entry)."""
    text = (text or "").strip()
    if not text:
        return "Nothing to save — the entry is empty.", None
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "Date must be YYYY-MM-DD.", None
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        return "Time must be HH:MM.", None

    existing = db.get_daily_observations(user_id, date_str) or {}
    prev = (existing.get(field) or "").strip()
    entry = f"[{time_str}] {text}"
    merged = f"{prev}\n{entry}" if prev else entry
    db.upsert_daily_observations(user_id, {"date": date_str, field: merged})
    return None, entry


@app.route("/note/quick", methods=["POST"])
def note_quick():
    """Nav note button: append a timestamped entry to the chosen day's notes."""
    payload = request.get_json(force=True, silent=True) or request.form
    date_str = (payload.get("date") or date.today().isoformat()).strip()
    time_str = (payload.get("time") or datetime.now().strftime("%H:%M")).strip()
    error, entry = _append_log_entry(uid(), date_str, time_str, payload.get("text"), "notes")
    if error:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "date": date_str, "entry": entry})


def _parse_log_entries(log: str) -> list:
    """Split a stored log field into [{'time', 'text'}] entries."""
    entries = []
    for line in (log or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            t, rest = line[1:].split("]", 1)
            entries.append({"time": t.strip(), "text": rest.strip()})
        else:
            entries.append({"time": None, "text": line})
    return entries


@app.route("/stress", methods=["GET"])
def stress_log():
    """Stressors log: timestamped entries, newest day first."""
    all_obs = db.get_all_daily_observations(uid()) or []
    days = []
    for obs in reversed(all_obs):  # date DESC
        log = (obs.get("stressors") or "").strip()
        if not log:
            continue
        days.append({"date": obs["date"], "entries": _parse_log_entries(log)})
    days = days[:60]  # newest first, capped
    now = datetime.now()
    return render_template(
        "stress_log.html",
        days=days,
        today=now.strftime("%Y-%m-%d"),
        now_time=now.strftime("%H:%M"),
        error=request.args.get("error"),
    )


@app.route("/stress/quick", methods=["POST"])
def stress_quick():
    """Stressors log: append a timestamped entry to the chosen day."""
    form = request.form
    date_str = (form.get("date") or date.today().isoformat()).strip()
    time_str = (form.get("time") or datetime.now().strftime("%H:%M")).strip()
    error, _ = _append_log_entry(uid(), date_str, time_str, form.get("text"), "stressors")
    if error:
        return redirect(url_for("stress_log", error=error))
    return redirect(url_for("stress_log"))


# ============================================================
# Mobile Quick Log & Status
# ============================================================

@app.route("/mobile/log", methods=["GET", "POST"])
@login_required
def mobile_log():
    """Ultra-minimal daily entry for mobile quick logging."""
    today = date.today().isoformat()
    existing = db.get_daily_observations(uid(), today)

    if request.method == "POST":
        form = request.form

        def get_bool(key):
            return 1 if form.get(key) == "on" else 0

        def get_float(key):
            val = form.get(key, "").strip()
            try:
                return float(val) if val else None
            except ValueError:
                return None

        # Only include fields present on this form — merge, don't overwrite
        data = {"date": today}

        # Biometrics — only include if field was submitted with a value
        for field in ("hours_slept", "hrv", "hrv_rmssd", "basal_temp_delta",
                      "steps", "sun_exposure_min"):
            val = get_float(field)
            if val is not None:
                data[field] = val

        # UV protection
        uv_prot = form.get("uv_protection_level")
        if uv_prot:
            data["uv_protection_level"] = uv_prot

        # Symptoms — always include (unchecked = 0, checked = 1)
        for sym in ("neurological", "cognitive", "musculature", "migraine",
                    "pulmonary", "dermatological", "rheumatic", "mucosal", "gastro"):
            data[sym] = get_bool(sym)

        # Pain + fatigue
        for field in ("pain_scale", "fatigue_scale", "fatigue_morning", "fatigue_evening"):
            val = get_float(field)
            if val is not None:
                data[field] = val

        # Flare
        data["flare_occurred"] = get_bool("flare_occurred")
        if data["flare_occurred"]:
            data["flare_severity"] = _clean_flare_severity(form.get("flare_severity"))

        db.upsert_daily_observations(uid(), data)
        return redirect(url_for("mobile_status"))

    return render_template("mobile_log.html", entry_date=today, existing=existing)


@app.route("/mobile/status")
@login_required
def mobile_status():
    """Mobile home screen — current risk at a glance."""
    all_obs = db.get_all_daily_observations(uid())
    if not all_obs or len(all_obs) < 7:
        return render_template("mobile_status.html", has_data=False)

    all_obs.sort(key=lambda x: x['date'], reverse=True)
    _inject_cycle_phase(all_obs)

    obs_by_date = {o['date']: o for o in all_obs}
    _inject_scoring_context(all_obs, obs_by_date, get_location_key(), n=14)

    scores = [calculate_flare_prime_score(obs) for obs in all_obs[:14]]
    today_score = scores[0]

    _fw = get_current_weights(uid())
    threshold = _fw.get('flare_threshold', 8.0)
    risk_info = get_risk_level(today_score, threshold)
    factors = get_contributing_factors(all_obs[0])
    score_delta = round(today_score - scores[1], 1) if len(scores) >= 2 else None

    # 14-day risk strip data
    risk_strip = []
    for i, obs in enumerate(all_obs[:14]):
        s = scores[i]
        if s >= threshold:
            color = '#c94040'
        elif s >= threshold * 0.65:
            color = '#d4a054'
        elif s >= threshold * 0.4:
            color = '#d4b84a'
        else:
            color = '#4a9e6e'
        risk_strip.append({
            'date': obs['date'],
            'score': s,
            'color': color,
            'flare': obs.get('flare_occurred') == 1,
        })

    return render_template(
        "mobile_status.html",
        has_data=True,
        today_score=round(today_score, 1),
        max_score=25,
        risk_level=risk_info['level'],
        risk_color=risk_info['color'],
        risk_description=risk_info['description'],
        predicted_flare=today_score >= threshold,
        score_delta=score_delta,
        factors=factors,
        risk_strip=list(reversed(risk_strip)),
        threshold=threshold,
    )
