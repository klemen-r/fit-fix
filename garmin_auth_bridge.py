"""Pipe-only Garmin authentication bridge for Cloudflare-blocked Rust SSO."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


def _read() -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line:
        raise RuntimeError("Authentication bridge input closed")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("Authentication bridge received invalid input")
    return value


def _write(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _tokens(client: Any) -> dict[str, str]:
    if not client.di_token or not client.di_refresh_token or not client.di_client_id:
        raise RuntimeError("Garmin login did not return reusable DI tokens")
    return {
        "access_token": client.di_token,
        "refresh_token": client.di_refresh_token,
        "client_id": client.di_client_id,
    }


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        from garminconnect.client import Client

        request = _read()
        email = request.get("email")
        password = request.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            raise RuntimeError("Email and password are required")

        client = Client()
        # Rust already tried mobile SSO. Start at the widget/browser bucket instead
        # of repeating the known-blocked mobile strategies.
        client.skip_strategies = {"mobile+cffi", "mobile+requests"}
        status, _state = client.login(email, password, return_on_mfa=True)
        password = ""
        request.clear()
        if status == "needs_mfa":
            _write({"status": "mfa_required"})
            request = _read()
            code = request.get("mfa_code")
            if not isinstance(code, str) or not code.strip():
                raise RuntimeError("MFA code is required")
            client.resume_login(None, code.strip())
            code = ""
            request.clear()

        _write({"status": "authenticated", "tokens": _tokens(client)})
        return 0
    except Exception as error:
        _write(
            {
                "status": "error",
                "kind": type(error).__name__,
                "message": str(error),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
