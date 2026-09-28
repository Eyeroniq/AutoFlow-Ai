"""SMTP/IMAP providers against in-process fakes of smtplib/imaplib (no network)."""

import base64
import email
import email.policy
import imaplib
import smtplib
from email.message import EmailMessage

import pytest

from flowforge_engine.errors import ProviderError
from flowforge_engine.providers import EmailAccount, EmailAttachment, MailboxQuery, OutgoingEmail, RetryPolicy
from flowforge_engine.providers.imap_provider import IMAPEmailProvider, _html_to_text, _search_criteria
from flowforge_engine.providers.smtp_provider import SMTPEmailProvider, build_message

APP_PASSWORD = "abcd efgh ijkl mnop"
ACCOUNT = EmailAccount(username="me@gmail.com", password=APP_PASSWORD)
FAST = RetryPolicy(max_retries=2, base_delay=0, max_delay=0)


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail_connects = 0
    reject_login = False

    def __init__(self, host, port, timeout=None, **kwargs):
        if FakeSMTP.fail_connects:
            FakeSMTP.fail_connects -= 1
            raise ConnectionRefusedError("connection refused")
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list[tuple[EmailMessage, str, list[str]]] = []
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}")
        self.password = password
        if FakeSMTP.reject_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted.")

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.sent.append((message, from_addr, list(to_addrs)))
        return {}

    def noop(self):
        self.calls.append("noop")
        return (250, b"OK")

    def quit(self):
        self.calls.append("quit")

    def close(self):
        self.calls.append("close")


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSMTP.instances, FakeSMTP.fail_connects, FakeSMTP.reject_login = [], 0, False


def smtp_provider(account=ACCOUNT):
    return SMTPEmailProvider(account, retry=FAST, smtp_factory=FakeSMTP)


class TestBuildMessage:
    def test_plain_html_attachments_and_hidden_bcc(self):
        message = build_message(
            OutgoingEmail(
                to=["a@example.com"], cc=["c@example.com"], bcc=["secret@example.com"],
                subject="Report", body="Plain body", html_body="<p><b>HTML</b> body</p>",
                attachments=[
                    EmailAttachment("notes.txt", "hello"),
                    EmailAttachment("pixel.png", base64.b64encode(b"\x89PNG....").decode(), encoding="base64"),
                ],
            ),
            sender="me@gmail.com",
            from_name="FlowForge",
        )
        assert message["From"] == "FlowForge <me@gmail.com>"
        assert message["To"] == "a@example.com" and message["Cc"] == "c@example.com"
        assert "Bcc" not in message and "secret@example.com" not in message.as_string()
        assert message["Message-ID"].endswith("@gmail.com>")

        parsed = email.message_from_bytes(message.as_bytes(), policy=email.policy.default)
        assert parsed.get_body(("plain",)).get_content().strip() == "Plain body"
        assert "<b>HTML</b>" in parsed.get_body(("html",)).get_content()
        attachments = {part.get_filename(): part for part in parsed.iter_attachments()}
        assert attachments["notes.txt"].get_content().strip() == "hello"
        assert attachments["pixel.png"].get_content_type() == "image/png"
        assert attachments["pixel.png"].get_payload(decode=True) == b"\x89PNG...."

    def test_bad_base64_is_rejected(self):
        with pytest.raises(ValueError, match="not valid base64"):
            build_message(
                OutgoingEmail(to=["a@example.com"], subject="s", body="b",
                              attachments=[EmailAttachment("x.bin", "***", encoding="base64")]),
                sender="me@gmail.com",
            )


class TestSMTPProvider:
    async def test_sends_over_starttls_with_app_password(self):
        receipt = await smtp_provider().send_email(OutgoingEmail(
            to=["a@example.com"], cc=["c@example.com"], bcc=["b@example.com"], subject="Hi", body="Body",
        ))
        [smtp] = FakeSMTP.instances
        assert (smtp.host, smtp.port) == ("smtp.gmail.com", 587)
        assert smtp.calls[:4] == ["ehlo", "starttls", "ehlo", "login:me@gmail.com"]
        assert smtp.password == "abcdefghijklmnop"  # the spaces Google displays are stripped
        [(message, from_addr, recipients)] = smtp.sent
        assert from_addr == "me@gmail.com"
        assert recipients == ["a@example.com", "c@example.com", "b@example.com"]
        assert receipt["status"] == "sent" and receipt["mock"] is False
        assert receipt["message_id"] == message["Message-ID"]
        assert receipt["bcc"] == ["b@example.com"] and receipt["from"] == "me@gmail.com"

    async def test_port_465_uses_implicit_tls(self):
        provider = smtp_provider(EmailAccount(username="me@gmail.com", password="pw", smtp_port=465))
        await provider.send_email(OutgoingEmail(to=["a@example.com"], subject="s", body="b"))
        [smtp] = FakeSMTP.instances
        assert smtp.port == 465 and "starttls" not in smtp.calls

    async def test_bad_login_explains_app_passwords(self):
        FakeSMTP.reject_login = True
        with pytest.raises(ProviderError, match="login rejected for me@gmail.com .*App Password") as info:
            await smtp_provider().send_email(OutgoingEmail(to=["a@example.com"], subject="s", body="b"))
        assert APP_PASSWORD not in str(info.value)
        assert len(FakeSMTP.instances) == 1  # auth failures aren't retried

    async def test_connection_errors_are_retried(self):
        FakeSMTP.fail_connects = 2
        receipt = await smtp_provider().send_email(OutgoingEmail(to=["a@example.com"], subject="s", body="b"))
        assert receipt["status"] == "sent" and len(FakeSMTP.instances) == 1

    async def test_connection_errors_give_up(self):
        FakeSMTP.fail_connects = 10
        with pytest.raises(ProviderError, match="cannot connect to smtp.gmail.com:587.*gave up after 3 attempts"):
            await smtp_provider().send_email(OutgoingEmail(to=["a@example.com"], subject="s", body="b"))

    async def test_verify_logs_in_without_sending(self):
        details = await smtp_provider().verify()
        [smtp] = FakeSMTP.instances
        assert "noop" in smtp.calls and smtp.sent == []
        assert details == {"smtp_server": "smtp.gmail.com:587", "security": "starttls", "username": "me@gmail.com"}


def raw_email(subject, sender="Alice <alice@example.com>", body="Hello there", html=None, attachment=None):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "me@gmail.com"
    message["Subject"] = subject
    message["Date"] = "Mon, 28 Sep 2026 10:00:00 +0000"
    message["Message-ID"] = f"<{subject.replace(' ', '')}@example.com>"
    if body is not None:
        message.set_content(body)
    if html:
        if body is None:
            message.set_content(html, subtype="html")
        else:
            message.add_alternative(html, subtype="html")
    if attachment:
        message.add_attachment(attachment, maintype="application", subtype="pdf", filename="invoice.pdf")
    return message.as_bytes()


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for the provider: UID SEARCH / FETCH / STORE."""

    mailbox: dict[int, tuple[bytes, set[str]]] = {}
    instances: list["FakeIMAP"] = []
    reject_login = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.literal = None
        self.commands: list[tuple] = []
        self.readonly = None
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        if FakeIMAP.reject_login:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.user = user
        return "OK", [b"Logged in"]

    def select(self, mailbox, readonly=False):
        self.selected, self.readonly = mailbox, readonly
        return "OK", [str(len(self.mailbox)).encode()]

    def uid(self, command, *args):
        self.commands.append((command, *args, self.literal))
        if command == "SEARCH":
            uids = sorted(self.mailbox)
            if "UNSEEN" in args:
                uids = [u for u in uids if "\\Seen" not in self.mailbox[u][1]]
            self.literal = None
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            uid_set, items = args
            data = []
            for uid in [int(u) for u in uid_set.split(",")]:
                raw, flags = self.mailbox[uid]
                if "HEADER" in items:
                    header = raw.split(b"\n\n", 1)[0] + b"\n\n"
                    meta = f"{uid} (UID {uid} FLAGS ({' '.join(sorted(flags))}) RFC822.SIZE {len(raw)} BODY[HEADER] {{{len(header)}}}"
                    data += [(meta.encode(), header), b")"]
                else:
                    data += [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
            return "OK", data
        if command == "STORE":
            for uid in [int(u) for u in args[0].split(",")]:
                self.mailbox[uid][1].add("\\Seen")
            return "OK", []
        raise AssertionError(command)

    def logout(self):
        return "BYE", []


@pytest.fixture(autouse=True)
def _mailbox():
    FakeIMAP.instances, FakeIMAP.reject_login = [], False
    FakeIMAP.mailbox = {
        101: (raw_email("Old news"), {"\\Seen"}),
        102: (raw_email("Invoice 7", body="Please pay", attachment=b"%PDF-1.4"), set()),
        103: (raw_email("Newsletter", sender="news@example.com", body=None, html="<h1>Hi</h1><p>Sale &amp; more</p>"), set()),
    }


def imap_provider():
    return IMAPEmailProvider(ACCOUNT, retry=FAST, imap_factory=FakeIMAP)


class TestIMAPProvider:
    async def test_fetches_unread_newest_first_without_marking_read(self):
        emails = await imap_provider().fetch_emails(MailboxQuery())
        assert [e["subject"] for e in emails] == ["Newsletter", "Invoice 7"]
        [imap] = FakeIMAP.instances
        assert imap.user == "me@gmail.com" and imap.readonly is True
        assert all("\\Seen" not in FakeIMAP.mailbox[uid][1] for uid in (102, 103))

        invoice = emails[1]
        assert invoice["from"] == "Alice <alice@example.com>" and invoice["from_address"] == "alice@example.com"
        assert invoice["to"] == ["me@gmail.com"]
        assert invoice["body_text"] == "Please pay" and invoice["unread"] is True
        assert invoice["attachments"] == [{"filename": "invoice.pdf", "content_type": "application/pdf", "size": 8}]
        assert invoice["date"] == "2026-09-28T10:00:00+00:00"
        assert invoice["message_id"] == "<Invoice7@example.com>"
        # HTML-only mail is converted to text.
        assert emails[0]["body_text"] == "Hi\nSale & more"

    async def test_max_results_keeps_the_newest(self):
        emails = await imap_provider().fetch_emails(MailboxQuery(unread_only=False, max_results=2))
        assert [e["uid"] for e in emails] == ["103", "102"]

    async def test_mark_as_read(self):
        await imap_provider().fetch_emails(MailboxQuery(max_results=1, mark_as_read=True))
        [imap] = FakeIMAP.instances
        assert imap.readonly is False
        assert "\\Seen" in FakeIMAP.mailbox[103][1] and "\\Seen" not in FakeIMAP.mailbox[102][1]

    async def test_bad_login(self):
        FakeIMAP.reject_login = True
        with pytest.raises(ProviderError, match="IMAP login rejected .*App Password"):
            await imap_provider().fetch_emails(MailboxQuery())

    async def test_verify(self):
        details = await imap_provider().verify()
        assert details == {"imap_server": "imap.gmail.com:993", "username": "me@gmail.com", "inbox_messages": 3}

    def test_search_criteria(self):
        terms, literal = _search_criteria(MailboxQuery(from_address="alice@example.com", subject='say "hi"'))
        assert terms == ["UNSEEN", "FROM", '"alice@example.com"', "SUBJECT", '"say \\"hi\\""']
        assert literal is None
        terms, literal = _search_criteria(MailboxQuery(unread_only=False, subject="Rechnung für Mai"))
        assert terms == ["SUBJECT"] and literal == "Rechnung für Mai".encode()
        assert _search_criteria(MailboxQuery(unread_only=False)) == (["ALL"], None)
        with pytest.raises(ValueError, match="only one"):
            _search_criteria(MailboxQuery(from_address="José", subject="Größe"))

    def test_html_to_text(self):
        assert _html_to_text("<style>x{}</style><p>One<br>Two</p><div>&lt;3</div>") == "One\nTwo\n<3"
