import imaplib
import socket
import ssl
import threading
import time
from collections.abc import Iterator

import pytest

from postcard.core.net import imap_session
from postcard.core.net.auth import MECHANISM_XOAUTH2, Credential
from postcard.core.net.imap_session import FetchedHeader, ImapError, ImapSession


class FakeImap:
    def __init__(
        self, search_reply=("OK", [b"4 9 20"]), fetch_reply=None, status_reply=None
    ):
        self.calls = []
        self.welcome = b"fake imap"
        self._search_reply = search_reply
        self._fetch_reply = fetch_reply
        self._status_reply = status_reply

    def uid(self, *args):
        self.calls.append(args)
        return self._search_reply

    def fetch(self, *args):
        self.calls.append(args)
        return self._fetch_reply

    def status(self, *args):
        self.calls.append(args)
        return self._status_reply

    def authenticate(self, mechanism, authobject):
        self.calls.append(("AUTHENTICATE", mechanism, authobject(b"")))

    def login(self, user, password):
        self.calls.append(("LOGIN", user, password))

    def starttls(self, ssl_context=None):
        self.calls.append(("STARTTLS", ssl_context))


def connect(monkeypatch, imap: FakeImap) -> ImapSession:
    monkeypatch.setattr(
        "postcard.core.net.imap_session.imaplib.IMAP4_SSL",
        lambda *args, **kwargs: imap,
    )
    session = ImapSession("imap.example.com", 993)
    session.connect()
    return session


def test_search_all_uids_uses_uid_search_all_and_returns_the_uids(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    assert session.search_all_uids() == {"4", "9", "20"}
    assert imap.calls == [("SEARCH", "ALL")]


# --- search failure messages ------------------------------------------------
# Each of these used to raise the same "malformed data" text, which said
# nothing about which of the four checks had tripped.


def test_a_non_ok_status_says_the_search_failed(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("NO", [b"nope"])))
    with pytest.raises(ImapError, match="search failed"):
        session.search_all_uids()


def test_a_non_list_payload_names_the_type_it_got(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("OK", "4 9")))
    with pytest.raises(ImapError, match="search returned str, not a list"):
        session.search_all_uids()


def test_a_non_bytes_item_is_reported_as_such(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("OK", [b"4", 9])))
    with pytest.raises(ImapError, match="non-bytes item"):
        session.search_all_uids()


def test_non_numeric_uids_are_listed(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("OK", [b"4 bogus"])))
    with pytest.raises(ImapError, match=r"non-numeric UIDs: \['bogus'\]"):
        session.search_all_uids()


# --- unseen_count -----------------------------------------------------------


def test_unseen_count_asks_for_status_and_reads_the_number(monkeypatch):
    imap = FakeImap(status_reply=("OK", [b'"[Gmail]/All Mail" (UNSEEN 12)']))
    session = connect(monkeypatch, imap)

    assert session.unseen_count("[Gmail]/All Mail") == 12
    # Quoted, so the space stays inside one token and the server doesn't say BAD.
    assert imap.calls == [('"[Gmail]/All Mail"', "(UNSEEN)")]


def test_a_refused_status_names_the_mailbox(monkeypatch):
    session = connect(monkeypatch, FakeImap(status_reply=("NO", [b"unavailable"])))
    with pytest.raises(ImapError, match="could not read the status of Junk"):
        session.unseen_count("Junk")


def test_a_status_reply_without_an_unseen_field_is_an_error(monkeypatch):
    session = connect(monkeypatch, FakeImap(status_reply=("OK", [b'"X" (MESSAGES 9)'])))
    with pytest.raises(ImapError, match="no UNSEEN"):
        session.unseen_count("X")


def test_an_empty_status_reply_is_an_error(monkeypatch):
    session = connect(monkeypatch, FakeImap(status_reply=("OK", [None])))
    with pytest.raises(ImapError, match="no UNSEEN"):
        session.unseen_count("X")


# --- fetch_recent_headers ---------------------------------------------------

HEADER_BYTES = (
    b"Date: Thu, 16 Jul 2026 10:00:00 +0000\r\n"
    b"From: Ada Lovelace <ada@example.com>\r\n"
    b"To: Me <me@example.com>\r\n"
    b"Subject: Lunch\r\n"
    b"Message-ID: <a@example.com>\r\n"
    b"\r\n"
)


def test_fetch_recent_headers_returns_typed_rows(monkeypatch):
    # Arrange: one message, seen but not flagged, plus the stray ")" line
    # imaplib appends as plain bytes rather than a tuple.
    reply = (
        "OK",
        [(b"1 (UID 42 FLAGS (\\Seen) BODY[HEADER.FIELDS ...]", HEADER_BYTES), b")"],
    )
    session = connect(monkeypatch, FakeImap(fetch_reply=reply))

    # Act
    (header,) = session.fetch_recent_headers(exists=1, limit=50)

    # Assert: raw wire values, not display-ready ones -- the date is still the
    # original RFC 5322 string and the address is unparsed.
    assert isinstance(header, FetchedHeader)
    assert header.uid == "42"
    assert header.from_header == "Ada Lovelace <ada@example.com>"
    assert header.subject == "Lunch"
    assert header.date == "Thu, 16 Jul 2026 10:00:00 +0000"
    assert header.seen is True
    assert header.flagged is False


def test_a_missing_header_reads_back_as_empty(monkeypatch):
    reply = ("OK", [(b"1 (UID 7 FLAGS ()", b"Subject: Hi\r\n\r\n")])
    session = connect(monkeypatch, FakeImap(fetch_reply=reply))

    (header,) = session.fetch_recent_headers(exists=1, limit=50)

    assert header.cc_header == ""
    assert header.in_reply_to == ""
    assert header.references == ""
    assert header.seen is False


def test_an_empty_mailbox_is_not_fetched_at_all(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    assert session.fetch_recent_headers(exists=0, limit=50) == []
    assert imap.calls == []


def test_paging_past_the_oldest_message_returns_nothing(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    assert session.fetch_recent_headers(exists=10, limit=50, offset=10) == []
    assert imap.calls == []


# --- append -----------------------------------------------------------------


class AppendingImap(FakeImap):
    def __init__(self, reply=("OK", [b"[APPENDUID 1 7] APPEND completed"])):
        super().__init__()
        self._reply = reply
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1")

    def append(self, *args):
        self.calls.append(args)
        return self._reply


def test_append_quotes_the_mailbox_and_stores_the_copy_as_read(monkeypatch):
    # A Sent mailbox with a space in its name arrives as two tokens unless it
    # is quoted, and the server answers BAD.
    imap = AppendingImap()
    session = connect(monkeypatch, imap)

    session.append("[Gmail]/Sent Mail", b"raw")

    assert imap.calls == [('"[Gmail]/Sent Mail"', "\\Seen", None, b"raw")]


def test_append_raises_when_the_server_refuses(monkeypatch):
    session = connect(monkeypatch, AppendingImap(reply=("NO", [b"[TRYCREATE]"])))

    with pytest.raises(ImapError, match="could not append to Sent"):
        session.append("Sent", b"raw")


def test_has_capability_is_case_insensitive(monkeypatch):
    session = connect(monkeypatch, AppendingImap())

    assert session.has_capability("x-gm-ext-1")
    assert not session.has_capability("X-SOMETHING-ELSE")


def test_xoauth2_hands_imaplib_the_raw_sasl_response(monkeypatch):
    # imaplib base64-encodes the callback's return itself, so it has to get the
    # bytes unencoded.
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    session.sign_in(Credential("me@example.com", "token", MECHANISM_XOAUTH2))

    assert imap.calls == [
        ("AUTHENTICATE", "XOAUTH2", b"user=me@example.com\x01auth=Bearer token\x01\x01")
    ]


def test_a_password_account_still_uses_plain_login(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    session.sign_in(Credential("me@example.com", "hunter2"))

    assert imap.calls == [("LOGIN", "me@example.com", "hunter2")]


def test_an_unknown_mechanism_fails_rather_than_staying_unauthenticated(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    with pytest.raises(ImapError, match="unsupported mechanism none"):
        session.sign_in(Credential("me@example.com", "", "none"))

    assert imap.calls == []


# --- TLS -------------------------------------------------------------------
# imaplib defaults to ssl._create_stdlib_context(): check_hostname off,
# verify_mode CERT_NONE. Left alone, every sync accepts any certificate and
# hands the password to whoever is on the path.


def _assert_verifies(context: ssl.SSLContext) -> None:
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_connect_gives_imap4_ssl_a_verifying_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "postcard.core.net.imap_session.imaplib.IMAP4_SSL",
        lambda *args, **kwargs: captured.update(kwargs) or FakeImap(),
    )

    ImapSession("imap.example.com", 993).connect()

    _assert_verifies(captured["ssl_context"])


def test_connect_gives_starttls_a_verifying_context(monkeypatch):
    imap = FakeImap()
    monkeypatch.setattr(
        "postcard.core.net.imap_session.imaplib.IMAP4",
        lambda *args, **kwargs: imap,
    )

    ImapSession("imap.example.com", 143, "starttls").connect()

    assert imap.calls[0][0] == "STARTTLS"
    _assert_verifies(imap.calls[0][1])


# --- idle -------------------------------------------------------------------
# IDLE is driven through imaplib's internals (the stdlib has none of its own
# before 3.15), so the fake answers at that level rather than as a command.


class IdlingImap(FakeImap):
    """A server that reads back a scripted run of untagged lines.

    It stands in for the socket too: a line left in the script means the socket
    is ready, an empty script means a quiet mailbox.
    """

    def __init__(self, lines, response=None):
        super().__init__()
        self.lines = list(lines)
        self._response = response

    def socket(self):
        return self

    def _command(self, name):
        self.calls.append((name,))
        return b"TAG1"

    def _get_response(self):
        return self._response

    def _get_line(self):
        return self.lines.pop(0)

    def send(self, data):
        self.calls.append(("SEND", data))

    def _command_complete(self, name, tag):
        self.calls.append(("COMPLETE", name, tag))


def idling(monkeypatch, imap: IdlingImap) -> ImapSession:
    monkeypatch.setattr(
        imap_session.select,
        "select",
        lambda _r, _w, _x, _timeout: ([imap] if imap.lines else [], [], []),
    )
    return connect(monkeypatch, imap)


@pytest.mark.parametrize("line", [b"* 5 EXISTS", b"* 3 EXPUNGE", b"* 3 FETCH (FLAGS)"])
def test_idle_reports_the_untagged_replies_that_mean_news(monkeypatch, line):
    assert idling(monkeypatch, IdlingImap([line])).idle(60) is True


def test_a_quiet_idle_reports_no_change(monkeypatch):
    assert idling(monkeypatch, IdlingImap([])).idle(60) is False


def test_a_keepalive_is_not_news_and_the_idle_keeps_waiting(monkeypatch):
    imap = IdlingImap([b"* OK Still here"])
    session = idling(monkeypatch, imap)

    assert session.idle(60) is False
    assert imap.lines == []


def test_the_idle_is_always_ended(monkeypatch):
    imap = IdlingImap([b"* 5 EXISTS"])

    idling(monkeypatch, imap).idle(60)

    # Left running, it would answer the next command the session sends.
    assert ("SEND", b"DONE\r\n") in imap.calls
    assert ("COMPLETE", "IDLE", b"TAG1") in imap.calls


def test_a_server_that_refuses_to_idle_is_an_error(monkeypatch):
    # A tagged reply where imaplib reports the "+ idling" continuation as None.
    imap = IdlingImap([], response=b"TAG1 BAD no idling here")
    session = idling(monkeypatch, imap)

    with pytest.raises(ImapError, match="would not enter IDLE"):
        session.idle(60)
    assert ("SEND", b"DONE\r\n") not in imap.calls


# --- idle over a socket -----------------------------------------------------
# The fake above answers at imaplib's private API, which is the half of this
# that can drift between Python versions -- 3.13 and 3.14 already differ on
# what a timed out read does to a socket. So one test drives the whole thing
# over a real socket with real imaplib on top.

IDLE_SERVER_SCRIPT = {
    b"CAPABILITY": [b"* CAPABILITY IMAP4rev1 IDLE"],
    b"EXAMINE": [b"* 3 EXISTS"],
}


def serve_idle(server: socket.socket, pushes: Iterator[list[bytes]]) -> None:
    """Answer one client with just enough IMAP to be idled on.

    `pushes` yields the untagged lines to send during each IDLE in turn, each
    one a moment after the "+ idling" so it lands in its own packet -- the way
    a server that has been sitting there quietly sends it.
    """
    connection, _ = server.accept()
    with connection, connection.makefile("rwb") as stream:
        stream.write(b"* OK fake imap ready\r\n")
        stream.flush()
        while line := stream.readline():
            tag, _, rest = line.strip().partition(b" ")
            command = rest.split(b" ")[0].upper()
            if command == b"IDLE":
                stream.write(b"+ idling\r\n")
                stream.flush()
                for push in next(pushes, []):
                    time.sleep(0.05)
                    stream.write(push + b"\r\n")
                    stream.flush()
                while (done := stream.readline()).strip() != b"DONE":
                    if not done:
                        return
                stream.write(tag + b" OK IDLE terminated\r\n")
            elif command == b"LOGOUT":
                stream.write(b"* BYE\r\n" + tag + b" OK bye\r\n")
                stream.flush()
                return
            else:
                for untagged in IDLE_SERVER_SCRIPT.get(command, []):
                    stream.write(untagged + b"\r\n")
                stream.write(tag + b" OK " + command + b" done\r\n")
            stream.flush()


def test_a_quiet_idle_leaves_the_session_fit_for_the_next_one(monkeypatch):
    server = socket.create_server(("127.0.0.1", 0))
    pushes = iter([[], [b"* 5 EXISTS"]])
    thread = threading.Thread(target=serve_idle, args=(server, pushes), daemon=True)
    thread.start()

    monkeypatch.setattr(
        "postcard.core.net.imap_session.imaplib.IMAP4_SSL",
        lambda host, port, ssl_context=None, timeout=None: imaplib.IMAP4(
            host, port, timeout=timeout
        ),
    )
    session = ImapSession("127.0.0.1", server.getsockname()[1])
    session.connect()
    try:
        session.sign_in(Credential("ada@example.com", "hunter2"))
        assert session.has_capability("IDLE")
        session.select("INBOX")

        # The renewal every 20 minutes runs on this: a quiet idle that has to
        # leave the connection usable, or live sync costs a reconnect an hour.
        assert session.idle(0.2) is False
        assert session.idle(5) is True
    finally:
        session.logout()
        server.close()
