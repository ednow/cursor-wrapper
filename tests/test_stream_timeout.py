from app.stream_timeout import (
    content_idle_exceeded,
    queue_wait_seconds_for_content_idle,
    resolve_effective_stream_idle_seconds,
    response_ack_idle_force_remaining_seconds,
)


def test_resolve_idle_min_config_and_upstream_minus_margin() -> None:
    res = resolve_effective_stream_idle_seconds(
        120,
        {"X-Read-Timeout": "60"},
        margin_seconds=10,
        stream_id="resp_test",
    )
    assert res.effective == 50


def test_resolve_config_smaller_than_upstream_cap() -> None:
    res = resolve_effective_stream_idle_seconds(
        45,
        {"x-forwarded-read-timeout": "60"},
        margin_seconds=10,
    )
    assert res.effective == 45


def test_content_idle_exceeded() -> None:
    import time

    t0 = time.monotonic() - 50
    assert content_idle_exceeded(t0, 45) is True
    assert content_idle_exceeded(time.monotonic(), 45) is False
    assert content_idle_exceeded(t0, 0) is False


def test_queue_wait_uses_remaining_idle() -> None:
    import time

    t0 = time.monotonic() - 40
    wait = queue_wait_seconds_for_content_idle(12.0, t0, 45)
    assert 0 < wait <= 5.0


def test_response_ack_idle_force_remaining() -> None:
    import time

    t0 = time.monotonic() - 31
    assert (
        response_ack_idle_force_remaining_seconds(
            force_enabled=True,
            idle_seconds=30,
            wait_started_at=t0,
            ack_emitted=False,
            cursor_content_started=False,
        )
        == 0.0
    )
    assert (
        response_ack_idle_force_remaining_seconds(
            force_enabled=False,
            idle_seconds=30,
            wait_started_at=t0,
            ack_emitted=False,
            cursor_content_started=False,
        )
        is None
    )
    remaining = response_ack_idle_force_remaining_seconds(
        force_enabled=True,
        idle_seconds=30,
        wait_started_at=time.monotonic(),
        ack_emitted=False,
        cursor_content_started=False,
    )
    assert remaining is not None
    assert 0 < remaining <= 30.0
