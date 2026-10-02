"""Privacy layer: every detector (with false-positive cases), the checksums, the guard's
three modes on real nodes, PII Redact/Restore, the Secret Scanner, and the ICS Calendar node."""

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from flowforge_engine import ExecutionServices, LocalFileStore, NodeStatus, ProviderSettings, execute_node
from flowforge_engine.privacy import (
    ALL_CATEGORIES,
    MemoryVault,
    PrivacyPolicy,
    describe,
    guard,
    luhn_valid,
    mask,
    pseudonymize,
    restore,
    scan,
    scan_text,
    verhoeff_digit,
    verhoeff_valid,
)
from flowforge_engine.providers import MockEmailProvider, MockTelegramProvider
from flowforge_engine.providers.telegram_provider import TelegramProvider
from flowforge_engine.testing import make_context, mock_services, node

# Made-up but valid test values (never real people's).
CARD = "4111 1111 1111 1111"  # the standard Visa test number (passes Luhn)
AADHAAR_BASE = "23456789012"
AADHAAR = f"{AADHAAR_BASE[:4]} {AADHAAR_BASE[4:8]} {AADHAAR_BASE[8:]}{verhoeff_digit(AADHAAR_BASE)}"
PAN = "ABCPE1234F"


def types(text, categories=ALL_CATEGORIES, **kwargs):
    return [(f.type, text[f.start:f.end]) for f in scan_text(text, categories=categories, **kwargs)]


# --- Checksums -------------------------------------------------------------------------------


def test_verhoeff_accepts_valid_aadhaar_and_rejects_one_wrong_digit():
    digits = AADHAAR.replace(" ", "")
    assert verhoeff_valid(digits)
    wrong = digits[:-1] + str((int(digits[-1]) + 1) % 10)
    assert not verhoeff_valid(wrong)
    swapped = digits[:3] + digits[4] + digits[3] + digits[5:]
    assert digits == swapped or not verhoeff_valid(swapped)  # Verhoeff catches transpositions


def test_luhn():
    assert luhn_valid("4111111111111111") and luhn_valid("5555555555554444")
    assert not luhn_valid("4111111111111112")


# --- Detectors ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "kind"), [
    ("aws AKIAIOSFODNN7EXAMPLE here", "AWS_ACCESS_KEY"),
    ("key AIzaSyA1234567890abcdefghijklmnopqrstuv", "GOOGLE_API_KEY"),
    ("ghp_" + "a1B2" * 9, "GITHUB_TOKEN"),
    ("sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc", "STRIPE_KEY"),
    ("xoxb-123456789012-abcdefghij", "SLACK_TOKEN"),
    ("sk-proj-" + "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z", "OPENAI_KEY"),
    ("sk-ant-api03-" + "x" * 30, "ANTHROPIC_KEY"),
    ("gsk_" + "Ab12" * 12, "GROQ_KEY"),
    ("123456789:AA" + "B" * 33, "TELEGRAM_BOT_TOKEN"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "JWT"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "PRIVATE_KEY"),
    ("password=Hunter2isNotSafe", "PASSWORD"),
    ("postgresql://app:S3cr3tPass@db.internal:5432/prod", "CONNECTION_STRING"),
    ("auth token: Zx81kLmQ9pR7sT2vW4yB6nD0q", "PASSWORD"),
    ("my secret is Zx81kLmQ9pR7sT2vW4yB6nD0q", "HIGH_ENTROPY_SECRET"),
])
def test_secret_detectors(text, kind):
    assert kind in [t for t, _ in types(text, {"secret"})]


def test_a_connection_string_flags_only_the_password():
    [(kind, value)] = types("postgres://app:S3cr3tPass@db:5432/x", {"secret"})
    assert (kind, value) == ("CONNECTION_STRING", "S3cr3tPass")


@pytest.mark.parametrize("text", [
    "request id 3f2a9c1e-1b2c-4d5e-8f90-123456789abc",  # UUID
    "token 3f2a9c1e-1b2c-4d5e-8f90-123456789abc",  # UUID right after a keyword
    "commit 9b74c9897bac770ffc029102a200c5de9b74c989 and secret key sha 9b74c9897bac770ffc029102a200c5de",  # hex digests
    "aGVsbG8gd29ybGQgdGhpcyBpcyBqdXN0IGJhc2U2NA==",  # base64 with no keyword near it
    "the key to success is consistency, not a secret formula",  # keywords, ordinary words
    "phone the order 4111 1111 1111 1112 again",  # fails Luhn
    "1111 1111 1111 1111",  # Luhn-valid but one repeated digit
    "invoice 2345 6789 0123",  # 12 digits that fail Verhoeff
    "ABCDE1234F",  # PAN shape but D isn't a holder type
])
def test_false_positives_are_left_alone(text):
    assert types(text, ALL_CATEGORIES - {"personal"}) == []


def test_financial_and_government_ids():
    text = f"Card {CARD}, Aadhaar {AADHAAR}, PAN {PAN}."
    assert types(text, ALL_CATEGORIES - {"personal"}) == [("CREDIT_CARD", CARD), ("AADHAAR", AADHAAR), ("PAN", PAN)]
    assert ("AADHAAR", AADHAAR.replace(" ", "-")) in types(f"id {AADHAAR.replace(' ', '-')}")


def test_personal_rules_and_presidio():
    text = "Write to priya.raman@example.com or call +91 98765 43210. Rahul Sharma lives in Pune."
    found = types(text)
    assert ("EMAIL", "priya.raman@example.com") in found
    assert ("IN_PHONE", "+91 98765 43210") in found
    pytest.importorskip("presidio_analyzer")
    assert ("PERSON", "Rahul Sharma") in found and ("LOCATION", "Pune") in found


def test_labelled_names_on_forms_are_found():
    assert types("Name: Priya Deshmukh") == [("PERSON", "Priya Deshmukh")]
    assert types("Full name - Ravi Kumar Patil") == [("PERSON", "Ravi Kumar Patil")]
    assert types("the name: of the game") == [] and types("Customer: priya") == []


async def test_secret_scanner_summary_reads_like_a_sentence():
    result = await execute_node(node("scan", "secret_scanner", data=f"{CARD} and {PAN}"), make_context())
    assert result.output["summary"] == "1 card number, 1 PAN"
    clean = await execute_node(node("scan", "secret_scanner", data="hello"), make_context())
    assert clean.output["summary"] == "nothing sensitive"


def test_lowercase_words_are_not_taken_for_names():
    pytest.importorskip("presidio_analyzer")
    assert [t for t, _ in types("$ docker compose logs api | tail -4")] == []


def test_personal_data_only_when_asked():
    assert types("mail priya@example.com", {"secret", "financial", "government_id"}) == []


def test_allowlist_exact_and_regex():
    text = f"test card {CARD} and AKIAIOSFODNN7EXAMPLE"
    assert [t for t, _ in types(text, allowlist=[CARD])] == ["AWS_ACCESS_KEY"]
    assert types(text, allowlist=[CARD, r"re:AKIA[A-Z0-9]{16}"]) == []


def test_scan_walks_json_and_reports_paths_not_values():
    findings = scan({"to": "x@example.com", "body": f"pay {CARD}", "items": [{"note": PAN}]})
    assert [(f.type, f.path) for f in findings] == [("CREDIT_CARD", "body"), ("PAN", "items[0].note")]
    assert all(CARD not in json.dumps(f.as_dict()) for f in findings)


def test_distinct_counts_a_value_once_per_step():
    from flowforge_engine.privacy import distinct

    value = {"value": f"card {CARD}", "message": f"card {CARD}", "other": "card 5555 5555 5555 4444"}
    findings = scan(value)
    assert len(findings) == 3 and len(distinct(value, findings)) == 2


def test_mask_and_describe():
    masked, findings = mask({"body": f"card {CARD} and {AADHAAR}"})
    assert masked == {"body": "card [REDACTED:CREDIT_CARD] and [REDACTED:AADHAAR]"}
    assert describe(findings) == "1 card number, 1 Aadhaar number"


# --- Redact / Restore ----------------------------------------------------------------------------


def test_pseudonymize_and_restore_round_trip():
    text = "Rahul (rahul@example.com) emailed rahul@example.com about card " + CARD
    findings = scan_text(text, categories=ALL_CATEGORIES)
    redacted, mapping = pseudonymize(text, findings)
    assert "rahul@example.com" not in redacted and CARD not in redacted
    assert redacted.count("<EMAIL_1>") == 2 and "<CREDIT_CARD_1>" in redacted  # same value, same placeholder
    back, restored, unknown = restore(redacted + " <PERSON_9>", mapping)
    assert back == text + " <PERSON_9>" and unknown == ["<PERSON_9>"] and restored >= 3


async def test_redact_and_restore_nodes_through_the_vault():
    vault = MemoryVault()
    services = ExecutionServices(provider_settings=ProviderSettings(testing=True), vault=vault)
    text = f"Customer {CARD}, Aadhaar {AADHAAR}, email asha@example.com"
    red = await execute_node(node("pii_redact", "pii_redact", text=text), make_context(services=services))
    assert red.status == NodeStatus.SUCCESS
    out = red.output
    assert CARD not in json.dumps(out) and "asha@example.com" not in json.dumps(out)  # no values in the output
    assert out["mapping_id"] and set(out["by_type"]) >= {"CREDIT_CARD", "AADHAAR", "EMAIL"}
    answer = f"Reply to {out['placeholders'][-1]} regarding {out['placeholders'][0]}"
    back = await execute_node(
        node("pii_restore", "pii_restore", text=answer, mapping_id=out["mapping_id"]), make_context(services=services)
    )
    assert back.status == NodeStatus.SUCCESS and back.output["restored"] == 2
    assert "asha@example.com" in back.output["text"] or CARD in back.output["text"]
    gone = await execute_node(node("pii_restore", "pii_restore", text="x", mapping_id="nope"), make_context(services=services))
    assert gone.status == NodeStatus.FAILED and "expired" in gone.error


async def test_secret_scanner_node():
    result = await execute_node(
        node("scan", "secret_scanner", data={"msg": f"key AKIAIOSFODNN7EXAMPLE card {CARD}"}), make_context()
    )
    assert result.status == NodeStatus.SUCCESS and result.output["count"] == 2 and not result.output["clean"]
    assert {f["path"] for f in result.output["findings"]} == {"msg"}
    assert "AKIA" not in json.dumps(result.output)
    strict = await execute_node(
        node("scan", "secret_scanner", data=f"card {CARD}", fail_on_findings=True), make_context()
    )
    assert strict.status == NodeStatus.FAILED and strict.error == "Found 1 card number"


# --- Redact Image -------------------------------------------------------------------------------


def screenshot(tmp_path, lines, name="shot.png"):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1300, 60 + 55 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except OSError:
        pytest.skip("DejaVu font not available")
    for i, line in enumerate(lines):
        draw.text((30, 25 + i * 55), line, fill="black", font=font)
    path = tmp_path / name
    image.save(path)
    return path


async def test_redact_image_blacks_out_only_the_sensitive_words(tmp_path):
    pytest.importorskip("pytesseract")
    from PIL import Image

    path = screenshot(tmp_path, ["Payment details below", f"Card: {CARD}", "Thanks a lot"])
    files = LocalFileStore()
    stored = files.add(path, content_type="image/png")
    services = ExecutionServices(provider_settings=ProviderSettings(testing=True), files=files)
    result = await execute_node(node("r", "redact_image", image=stored.id, include_personal_data=False), make_context(services=services))
    assert result.status == NodeStatus.SUCCESS, result.error
    out = result.output
    assert out["summary"] == "1 card number" and out["hidden_words"] >= 1
    assert out["file"]["filename"] == "shot-redacted.png" and out["file"]["encoding"] == "base64"
    redacted = tmp_path / "out.png"
    redacted.write_bytes(base64.b64decode(out["file"]["content"]))
    with Image.open(redacted) as image:
        gray = image.convert("L")
        card_row = gray.crop((130, 88, 420, 106))  # inside where the card digits were
        first_row = gray.crop((30, 25, 400, 60))  # "Payment details below"
        assert min(card_row.getdata()) == 0 and sum(v < 30 for v in card_row.getdata()) > 0.5 * card_row.width * card_row.height
        assert sum(v < 30 for v in first_row.getdata()) < 0.3 * first_row.width * first_row.height  # still text, not a box


async def test_redact_image_refuses_non_images(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hi")
    files = LocalFileStore()
    stored = files.add(path, content_type="text/plain")
    services = ExecutionServices(provider_settings=ProviderSettings(testing=True), files=files)
    result = await execute_node(node("r", "redact_image", image=stored.id), make_context(services=services))
    assert result.status == NodeStatus.FAILED and "this node reads" in result.error


def test_base64_file_content_is_never_scanned_or_rewritten():
    attachment = {"filename": "x.png", "encoding": "base64", "content": "QUJDUEUxMjM0RiBBQkNQRTEyMzRG ABCPE1234F"}
    assert scan({"file": attachment}) == []
    assert mask({"file": attachment})[0] == {"file": attachment}
    assert [f.type for f in scan({"file": {**attachment, "encoding": "text"}})] == ["PAN"]


# --- The guard on real nodes ---------------------------------------------------------------------


def gmail(**config):
    return node("mail", "gmail", **{"auth": "mock", "to": "boss@example.com", "subject": "Order", **config})


@pytest.fixture(autouse=True)
def _clean_outboxes():
    MockEmailProvider.clear_outbox()
    MockTelegramProvider.clear_outbox()


async def test_guard_blocks_outbound_by_default_and_names_types_not_values():
    result = await execute_node(gmail(body=f"Card {CARD}, Aadhaar {AADHAAR}"), make_context())
    assert result.status == NodeStatus.FAILED
    assert "1 card number, 1 Aadhaar number in body" in result.error and CARD not in result.error
    assert result.privacy["action"] == "blocked" and result.privacy["by_type"] == {"CREDIT_CARD": 1, "AADHAAR": 1}
    assert MockEmailProvider.outbox() == []  # nothing was sent


async def test_guard_redact_sends_masked_data():
    result = await execute_node(gmail(body=f"Card {CARD}, Aadhaar {AADHAAR}", privacy_guard="redact"), make_context())
    assert result.status == NodeStatus.SUCCESS, result.error
    [sent] = MockEmailProvider.outbox()
    assert sent.body == "Card [REDACTED:CREDIT_CARD], Aadhaar [REDACTED:AADHAAR]"
    assert result.input["body"] == sent.body  # the step records what was actually sent
    assert result.privacy["action"] == "redacted"


async def test_guard_warn_sends_unchanged_and_records_it():
    result = await execute_node(gmail(body=f"Card {CARD}", privacy_guard="warn"), make_context())
    assert result.status == NodeStatus.SUCCESS
    assert MockEmailProvider.outbox()[0].body == f"Card {CARD}"
    assert result.privacy == {"mode": "warn", "action": "warned", "total": 1, "by_type": {"CREDIT_CARD": 1},
                              "by_category": {"financial": 1}, "fields": ["body"]}


async def test_guard_ignores_recipients_and_clean_content():
    result = await execute_node(gmail(body="All good", to="priya@example.com"), make_context())
    assert result.status == NodeStatus.SUCCESS and result.privacy["action"] == "clean"


async def test_guard_redacts_llm_prompts_by_default():
    result = await execute_node(
        node("gemini", "gemini", provider="mock", user_prompt=f"Summarize: customer card {CARD}"), make_context()
    )
    assert result.status == NodeStatus.SUCCESS
    assert result.input["user_prompt"] == "Summarize: customer card [REDACTED:CREDIT_CARD]"
    assert CARD not in result.output["response"]


async def test_guard_follows_the_workflow_policy_for_personal_data_and_allowlist():
    services = mock_services()
    services.privacy = PrivacyPolicy(detect_personal_data=True, allowlist=["boss@example.com"])
    blocked = await execute_node(gmail(body="Ask priya@example.com"), make_context(services=services))
    assert blocked.status == NodeStatus.FAILED and "email address" in blocked.error
    allowed = await execute_node(gmail(body="Ask boss@example.com"), make_context(services=services))
    assert allowed.status == NodeStatus.SUCCESS


def test_guard_off_does_nothing():
    config = {"privacy_guard": "off", "body": f"card {CARD}"}
    assert guard(config, ["body"])[0] is config


# --- ICS calendar -------------------------------------------------------------------------------


async def ics(**config):
    return await execute_node(node("ics", "ics_calendar", **config), make_context())


async def test_ics_event_parses_back_with_uid_alarm_and_attendees():
    from icalendar import Calendar

    result = await ics(title="Design review", start="2026-10-05T15:00", end="2026-10-05T16:30", location="Room 4",
                       description="Bring the mockups", attendees="asha@example.com, ravi@example.com")
    assert result.status == NodeStatus.SUCCESS, result.error
    out = result.output
    assert out["ambiguous"] is False and out["filename"] == "Design-review.ics" and out["content_type"] == "text/calendar"
    cal = Calendar.from_ical(out["content"])
    assert str(cal["method"]) == "PUBLISH"
    [event] = cal.walk("VEVENT")
    assert str(event["summary"]) == "Design review" and str(event["location"]) == "Room 4"
    start = event.decoded("dtstart")
    assert start.isoformat() == "2026-10-05T15:00:00+05:30"  # Asia/Kolkata by default
    assert event.decoded("dtend") - start == __import__("datetime").timedelta(minutes=90)
    assert sorted(str(a) for a in event.get("attendee")) == ["mailto:asha@example.com", "mailto:ravi@example.com"]
    [alarm] = event.walk("VALARM")
    assert alarm.decoded("trigger") == -__import__("datetime").timedelta(minutes=30)
    # Same title and start: the same UID (re-sending updates the event instead of duplicating it).
    again = await ics(title="Design review", start="2026-10-05T15:00")
    assert again.output["uid"] == out["uid"] and str(event["uid"]) == out["uid"]
    assert base64.b64decode(out["content_base64"]).decode() == out["content"]
    assert out["attachment"]["content"] == out["content"]


async def test_ics_duration_all_day_and_offsets():
    timed = await ics(title="Call", start="2026-10-05 09:00:00+00:00", duration_minutes=45)
    assert timed.output["end"] == "2026-10-05T09:45:00+00:00"
    day = await ics(title="Holiday", start="2026-10-02")
    assert day.output["all_day"] is True and day.output["end"] == "2026-10-03"


@pytest.mark.parametrize(("start", "end"), [("tomorrow evening", None), ("3pm", "4:30"), ("2026-10-05T15:00", "4:30"),
                                            ("2026-10-05T15:00", "2026-10-05T14:00"), ("2026-10-05", "2026-10-05T10:00")])
async def test_ics_refuses_to_guess(start, end):
    result = await ics(title="Lunch", start=start, end=end)
    assert result.status == NodeStatus.SUCCESS
    assert result.output["ambiguous"] is True and result.output["content"] == "" and result.output["reason"]


async def test_ics_rejects_bad_attendees_and_zones():
    bad = await ics(title="x", start="2026-10-05T10:00", attendees=["not-an-email"])
    assert bad.status == NodeStatus.FAILED and "not-an-email" in bad.error
    zone = await ics(title="x", start="2026-10-05T10:00", timezone="Mars/Olympus")
    assert zone.status == NodeStatus.FAILED and "unknown time zone" in zone.error


# --- Telegram documents and {{system.now}} ----------------------------------------------------


async def test_telegram_sends_the_ics_as_a_document_with_a_caption():
    event = await ics(title="Design review", start="2026-10-05T15:00")
    result = await execute_node(
        node("tg", "telegram", auth="mock", chat_id="42", text="Your invite", document=event.output["attachment"]),
        make_context(),
    )
    assert result.status == NodeStatus.SUCCESS, result.error
    [sent] = MockTelegramProvider.outbox()
    assert sent.payload["document"] == "Design-review.ics" and sent.payload["content_type"] == "text/calendar"
    assert "BEGIN:VCALENDAR" in sent.payload["content"] and sent.payload["caption"] == "Your invite"
    assert result.output["document"] == "Design-review.ics"


async def test_telegram_document_from_an_upload(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    files = LocalFileStore()
    stored = files.add(path, content_type="text/plain")
    services = ExecutionServices(provider_settings=ProviderSettings(testing=True), files=files)
    result = await execute_node(node("tg", "telegram", auth="mock", chat_id="1", document_file=stored.id),
                                make_context(services=services))
    assert result.status == NodeStatus.SUCCESS and MockTelegramProvider.outbox()[0].payload["size"] == 5


async def test_telegram_provider_posts_send_document_as_multipart():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    bot = TelegramProvider("123:abc", transport=httpx.MockTransport(handler))
    message = await bot.send_document("42", "a.ics", b"BEGIN:VCALENDAR", "text/calendar", caption="hi")
    assert message == {"message_id": 7}
    request = captured[0]
    assert request.url.path.endswith("/sendDocument")
    body = request.content.decode()
    assert 'name="document"; filename="a.ics"' in body and "BEGIN:VCALENDAR" in body and 'name="caption"' in body


async def test_system_now_and_today():
    result = await execute_node(node("t", "text", text="{{system.now}} | {{system.today}}"), make_context())
    now, today = result.output["text"].split(" | ")
    parsed = datetime.fromisoformat(now)
    assert parsed.tzinfo is not None and abs((datetime.now(UTC) - parsed).total_seconds()) < 60
    assert today == parsed.date().isoformat()
