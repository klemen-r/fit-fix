"""Offline tests for the pipe-only Garmin authentication bridge."""

from __future__ import annotations

import io
import json
import sys
import types
from typing import Any

import garmin_auth_bridge


class FakeClient:
    def __init__(self) -> None:
        self.skip_strategies: set[str] = set()
        self.di_token = "access"
        self.di_refresh_token = "refresh"
        self.di_client_id = "client"

    def login(
        self, email: str, password: str, *, return_on_mfa: bool
    ) -> tuple[str, None]:
        assert email == "user@example.com"
        assert password == "secret"
        assert return_on_mfa
        assert self.skip_strategies == {"mobile+cffi", "mobile+requests"}
        return "needs_mfa", None

    def resume_login(self, state: Any, code: str) -> tuple[None, None]:
        assert state is None
        assert code == "123456"
        return None, None


def test_bridge_completes_mfa_and_returns_tokens(monkeypatch: Any) -> None:
    fake_module = types.ModuleType("garminconnect.client")
    fake_module.Client = FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "garminconnect.client", fake_module)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            '{"email":"user@example.com","password":"secret"}\n'
            '{"mfa_code":"123456"}\n'
        ),
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)

    assert garmin_auth_bridge.main() == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages == [
        {"status": "mfa_required"},
        {
            "status": "authenticated",
            "tokens": {
                "access_token": "access",
                "refresh_token": "refresh",
                "client_id": "client",
            },
        },
    ]
