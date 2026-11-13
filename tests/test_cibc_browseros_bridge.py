import asyncio
import os
import pty
import termios
import time

import pytest

from scripts import cibc_browseros_bridge as bridge


def page(host, body=""):
    return f"[UNTRUSTED_PAGE_CONTENT origin=https://{host}]\n{body}"


@pytest.mark.parametrize(
    "host", ["secure.cibc.com.evil.test", "www.cibconline.cibc.com", "evil.test"]
)
def test_secret_origin(host):
    with pytest.raises(RuntimeError):
        bridge.require_origin(page(host), secret=True)


def test_login_uses_canonical_keys():
    current = page(
        bridge.LOGIN_HOST,
        '- textbox "Password (case sensitive)" [ref=e1]\n- button "Sign on" [ref=e2]',
    )
    signed = page(bridge.EXPORT_HOST, '- button "Sign out" [ref=e3]')
    session = Session([current, current, signed])
    asyncio.run(bridge.authenticate(session, 1, "user", "password"))
    keys = [a["key"] for n, a in session.calls if n == "act" and a["kind"] == "press"]
    assert keys == ["Escape", "Enter"]


def test_sole_card():
    one = '- option "CIBC MasterCard XXXX" [ref=e1]'
    assert bridge.mastercard_option(one) == "CIBC MasterCard XXXX"
    for body in ["", one + '\n- option "CIBC MasterCard YYYY" [ref=e2]']:
        with pytest.raises(RuntimeError):
            bridge.mastercard_option(body)


def test_actual_terminal_timeout_restores_echo():
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    start = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="Timed out"):
            bridge.read_verification_code(time.monotonic() + 0.08, fd=slave)
        assert time.monotonic() - start < 1
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


def test_terminal_timeout_discards_partial_secret():
    import threading

    master, slave = pty.openpty()
    previous = termios.tcgetattr(slave)

    def enter():
        while termios.tcgetattr(slave)[3] & termios.ECHO:
            time.sleep(0.001)
        os.write(master, b"123456")

    thread = threading.Thread(target=enter)
    thread.start()
    try:
        with pytest.raises(bridge.CibcError, match="Timed out"):
            bridge.read_verification_code(time.monotonic() + 0.2, fd=slave)
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert termios.tcgetattr(slave) == previous
        os.write(master, b"\n")
        assert os.read(slave, 1024) == b"\n"
    finally:
        os.close(master)
        os.close(slave)


def test_terminal_interrupt_restores_echo(monkeypatch):
    master, slave = pty.openpty()
    previous = termios.tcgetattr(slave)

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge.select, "select", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            bridge.read_verification_code(time.monotonic() + 1, fd=slave)
        assert termios.tcgetattr(slave) == previous
    finally:
        os.close(master)
        os.close(slave)


def test_hidden_terminal_input():
    import threading

    master, slave = pty.openpty()

    def enter():
        time.sleep(0.05)
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        os.write(master, b"bad\n123456\n")

    thread = threading.Thread(target=enter)
    thread.start()
    try:
        assert bridge.read_verification_code(time.monotonic() + 1, fd=slave) == "123456"
        assert termios.tcgetattr(slave)[3] & termios.ECHO
    finally:
        thread.join()
        os.close(master)
        os.close(slave)


def test_no_terminal(monkeypatch):
    monkeypatch.setattr(os, "isatty", lambda fd: False)
    with pytest.raises(RuntimeError, match="interactive"):
        bridge.read_verification_code(time.monotonic() + 1)


class Session:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.calls = []

    async def call(self, name, args, **kwargs):
        self.calls.append((name, args))
        if name == "snapshot":
            return next(self.snapshots)
        return ""


def test_authenticated_skips_code_and_alert(monkeypatch):
    monkeypatch.setattr(bridge, "send_alert", lambda _: pytest.fail("alert"))
    monkeypatch.setattr(
        bridge, "read_verification_code", lambda _: pytest.fail("input")
    )
    session = Session([page(bridge.EXPORT_HOST, '- button "Sign out" [ref=e1]')])
    asyncio.run(bridge.authenticate(session, 1, "user", "password"))
    assert not [c for c in session.calls if c[0] == "act"]


def test_code_once_and_missing_alert_warning(monkeypatch, caplog):
    current = page(
        bridge.LOGIN_HOST,
        '- textbox "Verification code" [ref=e1]\n- button "Continue" [ref=e2]',
    )
    signed = page(bridge.EXPORT_HOST, '- button "Sign out" [ref=e3]')
    session = Session([current, current, current, signed, signed])
    alerts = []
    monkeypatch.delenv("DISCORD_ALERT_URL", raising=False)
    monkeypatch.setattr(bridge, "send_alert", alerts.append)
    monkeypatch.setattr(bridge, "read_verification_code", lambda deadline: "123456")
    asyncio.run(bridge.authenticate(session, 1, "user", "password"))
    fills = [
        args for name, args in session.calls if name == "act" and args["kind"] == "fill"
    ]
    assert len(fills) == 1
    assert fills[0]["sensitive"] is True
    assert len(alerts) == 1 and "123456" not in alerts[0]
    assert "reminder not delivered" in caplog.text
    assert "123456" not in caplog.text


@pytest.mark.parametrize(
    "text",
    [
        "CAPTCHA",
        "invalid code",
        "code has expired",
        "date range is invalid",
        "not available for this account",
    ],
)
def test_site_errors(text):
    with pytest.raises(RuntimeError, match="site error"):
        bridge.check_page(page(bridge.LOGIN_HOST, text))


@pytest.mark.parametrize("fails", [False, True])
def test_task_tab_cleanup(tmp_path, monkeypatch, fails):
    from datetime import date

    class OwnedSession(Session):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call(self, name, args, **kwargs):
            if name == "tabs" and args["action"] == "new":
                self.calls.append((name, args))
                return "Created page 42"
            return await super().call(name, args, **kwargs)

    session = OwnedSession([page(bridge.EXPORT_HOST, '- button "Sign out" [ref=e1]')])

    async def authenticate(*args):
        if fails:
            raise bridge.CibcError("Synthetic failure")

    async def export(session, page_id, since, until, workdir):
        artifact = workdir / "cibc.csv"
        artifact.write_text("synthetic")
        return [artifact]

    monkeypatch.setattr(bridge, "authenticate", authenticate)
    monkeypatch.setattr(bridge, "export_activity", export)
    monkeypatch.setattr(bridge, "browseros_mcp_url", lambda: "http://127.0.0.1/mcp")
    if fails:
        with pytest.raises(bridge.CibcError):
            asyncio.run(
                bridge.run_download(
                    date(2026, 10, 1),
                    date(2026, 10, 31),
                    "u",
                    "p",
                    session_factory=lambda _: session,
                )
            )
    else:
        artifacts = asyncio.run(
            bridge.run_download(
                date(2026, 10, 1),
                date(2026, 10, 31),
                "u",
                "p",
                session_factory=lambda _: session,
            )
        )
        artifacts[0].unlink()
        artifacts[0].parent.rmdir()
    assert ("tabs", {"action": "close", "page": 42}) in session.calls
    assert not any(
        n == "tabs" and a.get("page") not in (None, 42) for n, a in session.calls
    )


def test_terminal_eof_restores_echo():
    import threading

    master, slave = pty.openpty()
    previous = termios.tcgetattr(slave)

    def enter():
        time.sleep(0.05)
        os.write(master, b"\x04")

    thread = threading.Thread(target=enter)
    thread.start()
    try:
        with pytest.raises(bridge.CibcError, match="input ended"):
            bridge.read_verification_code(time.monotonic() + 1, fd=slave)
        assert termios.tcgetattr(slave) == previous
    finally:
        thread.join()
        os.close(master)
        os.close(slave)


def test_export_sets_six_dates_and_csv(tmp_path, monkeypatch):
    from datetime import date

    names = [
        "Account:",
        "From month",
        "From day",
        "From year",
        "To month",
        "To day",
        "To year",
        "Financial management software:",
    ]
    body = "\n".join(f'- combobox "{name}" [ref=e{i}]' for i, name in enumerate(names))
    body += '\n- option "CIBC MasterCard XXXX" [ref=e20]\n- button "Download Transactions" [ref=e21]\n- generic "All within date range" [ref=e22]'
    current = page(bridge.EXPORT_HOST, body)
    session = Session([current] * 11)
    raw = tmp_path / "download.csv"
    raw.write_text("synthetic")
    monkeypatch.setattr(bridge, "download_path", lambda _: raw)
    result = asyncio.run(
        bridge.export_activity(
            session, 1, date(2026, 10, 1), date(2026, 10, 31), tmp_path / "private"
        )
    )
    selects = [
        a["value"] for n, a in session.calls if n == "act" and a["kind"] == "select"
    ]
    assert selects == [
        "CIBC MasterCard XXXX",
        "October",
        "1",
        "2026",
        "October",
        "31",
        "2026",
        "Spreadsheet (CSV)",
    ]
    assert any(
        n == "act" and a["kind"] == "click" and a["ref"] == "e22"
        for n, a in session.calls
    )
    assert result[0].exists() and not raw.exists()
