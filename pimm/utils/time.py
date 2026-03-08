from datetime import datetime, time, timedelta, timezone

# UTC+8 — Hong Kong / Singapore / Shanghai, no DST
HKT = timezone(timedelta(hours=8))


def now_hkt():
    return datetime.now(tz=HKT)


def session_window_to_times(window):
    # Convert SessionWindow to (start_time, end_time) pair
    return (
        time(window.start_hour, window.start_minute),
        time(window.end_hour, window.end_minute),
    )


def is_in_session(config, dt=None):
    # Check if dt falls within any configured session window
    if dt is None:
        dt = now_hkt()
    current_time = dt.time()
    for window in config.sessions:
        start, end = session_window_to_times(window)
        if start <= current_time < end:
            return True
    return False


def seconds_until_session_end(config, dt=None):
    # Seconds until current session ends, None if not in session
    if dt is None:
        dt = now_hkt()
    current_time = dt.time()
    for window in config.sessions:
        start, end = session_window_to_times(window)
        if start <= current_time < end:
            end_dt = dt.replace(
                hour=end.hour, minute=end.minute, second=0, microsecond=0
            )
            return (end_dt - dt).total_seconds()
    return None


def needs_refresh(last_sent, order_valid_time, refresh_buffer, now=None):
    # Check if a quote needs proactive refresh before expiry
    if last_sent is None:
        return True
    if now is None:
        now = now_hkt()
    elapsed = (now - last_sent).total_seconds()
    threshold = order_valid_time * 60 - refresh_buffer
    return elapsed >= threshold
