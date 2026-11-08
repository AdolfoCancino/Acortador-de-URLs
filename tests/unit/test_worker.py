import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.worker import event_to_row


def valid_event():
    return {
        "event_id": str(uuid4()),
        "link_id": 1,
        "ts": "2026-11-07T12:00:00+00:00",
        "country": "cl",
        "ip_hash": "a" * 64,
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        "referer": "https://referrer.example/",
    }


def test_worker_parses_user_agent_and_validates_event():
    event = valid_event()
    row = event_to_row(json.dumps(event))
    assert row["event_id"] == event["event_id"]
    assert row["ts"] == datetime(2026, 11, 7, 12, tzinfo=UTC)
    assert row["country"] == "CL"
    assert row["browser"] == "Mobile Safari"
    assert row["device"] == "iPhone"
    assert "user_agent" not in row
    assert "ip" not in row


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "not-a-uuid"},
        {"event_id": 123},
        {"event_id": []},
        {"event_id": None},
        {"event_id": {}},
        {"link_id": 0},
        {"link_id": True},
        {"link_id": 2**63},
        {"link_id": 1 << 100},
        {"ts": "2026-11-07T12:00:00"},
        {"ip_hash": "203.0.113.1"},
        {"country": "Chile"},
        {"user_agent": []},
        {"referer": {}},
    ],
)
def test_worker_rejects_malformed_events(overrides):
    event = {**valid_event(), **overrides}
    with pytest.raises((ValueError, TypeError)):
        event_to_row(json.dumps(event))
