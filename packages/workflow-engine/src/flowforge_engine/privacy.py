"""Finding secrets and personal data, and masking them: one service for every caller.

The privacy guard (outbound and LLM nodes, before they run), the Secret Scanner and PII
Redact nodes, and the API's masking of stored step inputs/outputs all call `scan` and
`redact` here; nothing else detects anything.

Findings carry a type, a category, where they are (a field path and character offsets), a
confidence, and the detector that found them; never the matched text. Callers that need to
replace text slice it from the value they already hold.

Detectors, by category:
- secret: provider key formats (AWS, Google, GitHub, Stripe, Slack, OpenAI, Anthropic, Groq,
  Telegram bot tokens), JWTs, private-key headers, `password=...`-style assignments,
  credentials in connection strings, and high-entropy tokens of 20+ characters right after a
  word like key, token, secret, or password (UUIDs and hex digests are left alone);
- financial: card numbers that pass the Luhn check and start like a real card network;
- government_id: Aadhaar numbers that pass the Verhoeff checksum, and PANs (the fourth letter
  must be a valid holder type; PANs have no public check digit);
- personal (only when asked: the workflow's "detect personal data" setting, or the
  node's): email addresses and Indian phone numbers by rule, plus names, locations, and other
  phone numbers from Microsoft Presidio with a small spaCy model, when it's installed.

A per-workflow allowlist suppresses known-safe matches: an entry matches a finding whose
text equals it (case-insensitive), or, written `re:<pattern>`, whose text matches the regex.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Category = Literal["secret", "financial", "government_id", "personal"]
GuardMode = Literal["off", "warn", "redact", "block"]
# What the guard and masking look for unless personal data is switched on.
DEFAULT_CATEGORIES: frozenset[str] = frozenset({"secret", "financial", "government_id"})
ALL_CATEGORIES: frozenset[str] = DEFAULT_CATEGORIES | {"personal"}
# Overlapping findings: the earlier category in this order wins.
_PRIORITY = {"secret": 0, "government_id": 1, "financial": 2, "personal": 3}

LABELS = {
    "AWS_ACCESS_KEY": "AWS access key", "GOOGLE_API_KEY": "Google API key", "GITHUB_TOKEN": "GitHub token",
    "STRIPE_KEY": "Stripe secret key", "SLACK_TOKEN": "Slack token", "OPENAI_KEY": "OpenAI key",
    "ANTHROPIC_KEY": "Anthropic key", "GROQ_KEY": "Groq key", "TELEGRAM_BOT_TOKEN": "Telegram bot token",
    "JWT": "JWT", "PRIVATE_KEY": "private key", "PASSWORD": "password", "CONNECTION_STRING": "database password",
    "HIGH_ENTROPY_SECRET": "secret-looking token", "CREDIT_CARD": "card number", "AADHAAR": "Aadhaar number",
    "PAN": "PAN", "EMAIL": "email address", "IN_PHONE": "Indian phone number", "PHONE": "phone number",
    "PERSON": "name", "LOCATION": "location", "IP_ADDRESS": "IP address", "IBAN": "IBAN",
}


@dataclass(frozen=True)
class Finding:
    type: str
    category: str
    start: int
    end: int
    confidence: float
    detector: str
    # Where in the scanned value: "" for a plain string, else e.g. "body" or "fields.Notes".
    path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- Checksums -------------------------------------------------------------------------------

_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_VERHOEFF_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_valid(digits: str) -> bool:
    """The Verhoeff check (Aadhaar's last digit)."""
    check = 0
    for i, ch in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(ch)]]
    return check == 0


def verhoeff_digit(digits: str) -> str:
    """The digit that makes `digits` + it Verhoeff-valid (for making test numbers)."""
    check = 0
    for i, ch in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[(i + 1) % 8][int(ch)]]
    return str(_VERHOEFF_INV[check])


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def shannon_entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(c / len(text) * math.log2(c / len(text)) for c in counts.values()) if text else 0.0


# --- Rule-based detectors ----------------------------------------------------------------------

# (type, compiled pattern, value group or 0, confidence)
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], int, float]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|$)"), 0, 0.99),
    ("AWS_ACCESS_KEY", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"), 0, 0.95),
    ("GOOGLE_API_KEY", re.compile(r"(?<![\w-])AIza[0-9A-Za-z_\-]{35}(?![\w-])"), 0, 0.95),
    ("GITHUB_TOKEN", re.compile(r"(?<!\w)(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"), 0, 0.97),
    ("STRIPE_KEY", re.compile(r"(?<!\w)[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"), 0, 0.95),
    ("SLACK_TOKEN", re.compile(r"(?<!\w)xox[abposr]-[A-Za-z0-9-]{10,}"), 0, 0.95),
    ("ANTHROPIC_KEY", re.compile(r"(?<![\w-])sk-ant-[A-Za-z0-9_\-]{20,}"), 0, 0.97),
    ("OPENAI_KEY", re.compile(r"(?<![\w-])sk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}"), 0, 0.9),
    ("GROQ_KEY", re.compile(r"(?<!\w)gsk_[A-Za-z0-9]{40,}"), 0, 0.95),
    ("TELEGRAM_BOT_TOKEN", re.compile(r"(?<![\w:])\d{8,10}:AA[A-Za-z0-9_-]{33}(?![\w-])"), 0, 0.95),
    ("JWT", re.compile(r"(?<![\w-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 0, 0.9),
    ("CONNECTION_STRING", re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqps?|mssql)(?:\+\w+)?://[^:/\s@]+:([^@\s]+)@"
    ), 1, 0.95),
    ("PASSWORD", re.compile(
        r"(?i)\b(?:pass(?:word|wd|phrase)?|pwd|secret|api[_-]?key|access[_-]?key|auth[_ -]?token|client[_-]?secret)\b"
        r"[\"']?\s*[:=]\s*[\"']?([^\s\"',;&]{6,})"
    ), 1, 0.85),
]
_KEYWORD = re.compile(r"(?i)\b(?:api[_ -]?)?(?:key|token|secret|password|passwd|pwd|credential|bearer)s?\b")
_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-.]{20,}")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX = re.compile(r"^[0-9a-fA-F]+$")

_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d])")
_CARD_PREFIX = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[47]|6(?:011|5|4[4-9]|22)|3(?:0[0-5]|[68])|35)")
_AADHAAR = re.compile(r"(?<![\d-])[2-9]\d{3}([ -]?)\d{4}\1\d{4}(?![\d])")
_PAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z](?![A-Za-z0-9])")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
# A name written as a labelled field ("Name: Priya Deshmukh", "Full name - Ravi K"): forms and
# screenshots, where a small NER model often misses it. Only the value is flagged.
_LABELLED_NAME = re.compile(
    r"(?i:\b(?:full\s+name|name|customer|patient|applicant|account\s+holder|holder)\b)\s*[:\-–]\s*"
    r"([A-Z][a-z'’.]+(?:[ \t]+[A-Z][a-z'’.]*){0,3})"
)
_IN_PHONE = re.compile(r"(?<![\w+])(?:(?:\+|00)91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")


def _secret_findings(text: str) -> list[Finding]:
    found: list[Finding] = []
    for kind, pattern, group, confidence in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(group)
            found.append(Finding(kind, "secret", start, end, confidence, "pattern"))
    # High-entropy tokens just after a keyword ("api key: ...", "token=...", "Bearer ...").
    for keyword in _KEYWORD.finditer(text):
        window_end = min(len(text), keyword.end() + 40)
        token = _TOKEN.search(text, keyword.end(), window_end)
        if token is None:
            continue
        value = token.group(0).rstrip(".")
        lead = len(value) - len(value.lstrip("=:-."))
        value = value[lead:]
        if len(value) < 20 or _UUID.match(value) or _HEX.match(value):
            continue  # ids and digests (commit SHAs, checksums), not secrets
        classes = sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[_\-+/=]"))
        if classes >= 3 and shannon_entropy(value) >= 3.5:
            start = token.start() + lead
            found.append(Finding("HIGH_ENTROPY_SECRET", "secret", start, start + len(value), 0.7, "entropy"))
    return found


def _financial_findings(text: str) -> list[Finding]:
    found = []
    for match in _CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _CARD_PREFIX.match(digits) and luhn_valid(digits) and len(set(digits)) > 1:
            found.append(Finding("CREDIT_CARD", "financial", *match.span(), 0.95, "luhn"))
    return found


def _government_findings(text: str) -> list[Finding]:
    found = []
    for match in _AADHAAR.finditer(text):
        if verhoeff_valid(re.sub(r"\D", "", match.group(0))):
            found.append(Finding("AADHAAR", "government_id", *match.span(), 0.95, "verhoeff"))
    for match in _PAN.finditer(text):
        found.append(Finding("PAN", "government_id", *match.span(), 0.85, "pattern"))
    return found


def _personal_rule_findings(text: str) -> list[Finding]:
    found = [Finding("EMAIL", "personal", *m.span(), 0.95, "pattern") for m in _EMAIL.finditer(text)]
    found += [Finding("IN_PHONE", "personal", *m.span(), 0.8, "pattern") for m in _IN_PHONE.finditer(text)]
    found += [Finding("PERSON", "personal", *m.span(1), 0.75, "pattern") for m in _LABELLED_NAME.finditer(text)]
    return found


# --- Presidio --------------------------------------------------------------------------------

# Presidio entity -> our type. Its email, credit-card, and Indian ID recognizers are left to
# the rules above (which validate checksums), so it only adds what rules can't find.
_PRESIDIO_TYPES = {"PERSON": "PERSON", "LOCATION": "LOCATION", "PHONE_NUMBER": "PHONE", "IP_ADDRESS": "IP_ADDRESS",
                   "IBAN_CODE": "IBAN"}
PRESIDIO_MIN_SCORE = 0.5
_analyzer: Any = None
_analyzer_error: str | None = None
_analyzer_lock = threading.Lock()


def presidio_status() -> str | None:
    """None when Presidio loaded (or hasn't been needed yet), else why it's unavailable."""
    return _analyzer_error


def _presidio() -> Any:
    """The shared AnalyzerEngine (en_core_web_sm), loaded once; None if unavailable."""
    global _analyzer, _analyzer_error
    if _analyzer is not None or _analyzer_error is not None:
        return _analyzer
    with _analyzer_lock:
        if _analyzer is None and _analyzer_error is None:
            try:
                # Its loader warns about every recognizer of a language we don't load.
                logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)
                from presidio_analyzer import AnalyzerEngine
                from presidio_analyzer.nlp_engine import NlpEngineProvider

                provider = NlpEngineProvider(nlp_configuration={
                    "nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
                })
                _analyzer = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])
            except Exception as exc:  # not installed, or the spaCy model is missing
                _analyzer_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Presidio unavailable; personal data uses rules only", extra={"error": _analyzer_error})
    return _analyzer


def _presidio_findings(text: str) -> list[Finding]:
    analyzer = _presidio()
    if analyzer is None or not text.strip():
        return []
    results = analyzer.analyze(text=text, language="en", entities=list(_PRESIDIO_TYPES))
    return [
        Finding(_PRESIDIO_TYPES[r.entity_type], "personal", r.start, r.end, round(float(r.score), 3), "presidio")
        for r in results if r.score >= PRESIDIO_MIN_SCORE and r.entity_type in _PRESIDIO_TYPES
        # The small model tags lowercase words ("docker") as people; real names are capitalized.
        and not (r.entity_type in ("PERSON", "LOCATION") and not any(ch.isupper() for ch in text[r.start:r.end]))
    ]


# --- Scanning --------------------------------------------------------------------------------


class PrivacyPolicy(BaseModel):
    """A workflow's privacy settings (the API reads them from the workflow for each run)."""

    mask_stored_io: bool = Field(default=True, description="Store step inputs/outputs with findings masked.")
    detect_personal_data: bool = Field(default=False, description="Also find names, emails, phones, addresses.")
    allowlist: list[str] = Field(default_factory=list, max_length=200)

    @property
    def categories(self) -> frozenset[str]:
        return ALL_CATEGORIES if self.detect_personal_data else DEFAULT_CATEGORIES


def _allowed(text: str, allowlist: Iterable[str]) -> bool:
    for entry in allowlist:
        if entry.startswith("re:"):
            try:
                if re.fullmatch(entry[3:], text):
                    return True
            except re.error:
                continue
        elif entry.strip().lower() == text.strip().lower():
            return True
    return False


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Keep the best of overlapping findings: higher-priority category, then longer, then
    more confident; returned in text order."""
    ranked = sorted(findings, key=lambda f: (_PRIORITY[f.category], -(f.end - f.start), -f.confidence, f.start))
    kept: list[Finding] = []
    for finding in ranked:
        if all(finding.end <= k.start or finding.start >= k.end for k in kept):
            kept.append(finding)
    return sorted(kept, key=lambda f: f.start)


def scan_text(
    text: str, *, categories: Iterable[str] = DEFAULT_CATEGORIES, allowlist: Iterable[str] = (), path: str = "",
) -> list[Finding]:
    """Findings in one string, without overlaps, in text order."""
    wanted = set(categories)
    if not text:
        return []
    found: list[Finding] = []
    if "secret" in wanted:
        found += _secret_findings(text)
    if "financial" in wanted:
        found += _financial_findings(text)
    if "government_id" in wanted:
        found += _government_findings(text)
    if "personal" in wanted:
        found += _personal_rule_findings(text) + _presidio_findings(text)
    allow = list(allowlist)
    if allow:
        found = [f for f in found if not _allowed(text[f.start:f.end], allow)]
    return [Finding(f.type, f.category, f.start, f.end, f.confidence, f.detector, path) for f in _resolve_overlaps(found)]


def _walk(value: Any, path: str) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        # A file's base64 content isn't text: a random run of it can look like a PAN or a card
        # number, and "redacting" it would corrupt the file.
        binary = value.get("encoding") == "base64"
        for key, item in value.items():
            if binary and key == "content":
                continue
            yield from _walk(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def scan(
    value: Any, *, categories: Iterable[str] = DEFAULT_CATEGORIES, allowlist: Iterable[str] = (), path: str = "",
) -> list[Finding]:
    """Findings in every string inside `value` (dicts and lists are walked)."""
    cats, allow = list(categories), list(allowlist)
    return [f for p, text in _walk(value, path) for f in scan_text(text, categories=cats, allowlist=allow, path=p)]


def mask_placeholder(finding: Finding) -> str:
    return f"[REDACTED:{finding.type}]"


def _replace(text: str, findings: list[Finding], placeholder: Callable[[Finding, str], str]) -> str:
    out, last = [], 0
    for f in sorted(findings, key=lambda f: f.start):
        out += [text[last:f.start], placeholder(f, text[f.start:f.end])]
        last = f.end
    return "".join(out) + text[last:]


def redact(
    value: Any, findings: list[Finding], placeholder: Callable[[Finding, str], str] | None = None, path: str = "",
) -> Any:
    """`value` with every finding (from `scan` on the same value) replaced by its
    placeholder; the default is "[REDACTED:<TYPE>]". `placeholder(finding, original)`."""
    make = placeholder or (lambda f, _original: mask_placeholder(f))
    by_path: dict[str, list[Finding]] = {}
    for f in findings:
        by_path.setdefault(f.path, []).append(f)

    def walk(item: Any, here: str) -> Any:
        if isinstance(item, str):
            return _replace(item, by_path[here], make) if here in by_path else item
        if isinstance(item, dict):
            return {k: walk(v, f"{here}.{k}" if here else str(k)) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v, f"{here}[{i}]") for i, v in enumerate(item)]
        if isinstance(item, tuple):
            return tuple(walk(v, f"{here}[{i}]") for i, v in enumerate(item))
        return item

    return walk(value, path) if findings else value


def mask(value: Any, policy: PrivacyPolicy | None = None) -> tuple[Any, list[Finding]]:
    """(`value` with findings replaced by "[REDACTED:<TYPE>]", the findings)."""
    policy = policy or PrivacyPolicy()
    findings = scan(value, categories=policy.categories, allowlist=policy.allowlist)
    return redact(value, findings), findings


def summarize(findings: Iterable[Finding]) -> dict[str, Any]:
    """Counts only: {"total", "by_type", "by_category"}."""
    items = list(findings)
    return {
        "total": len(items),
        "by_type": dict(Counter(f.type for f in items)),
        "by_category": dict(Counter(f.category for f in items)),
    }


def distinct(value: Any, findings: Iterable[Finding]) -> list[Finding]:
    """One finding per distinct (type, matched text) in `value`: the same card number under
    two keys counts once. Uses the text only in memory; returns findings, not values."""
    texts = dict(_walk(value, ""))
    seen: set[tuple[str, str]] = set()
    unique = []
    for f in findings:
        key = (f.type, texts.get(f.path, "")[f.start:f.end])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def describe(findings: Iterable[Finding]) -> str:
    """"2 card numbers, 1 Aadhaar number": for messages (no values)."""
    counts = Counter(f.type for f in findings)
    parts = []
    for kind, count in counts.most_common():
        label = LABELS.get(kind, kind.lower().replace("_", " "))
        parts.append(f"{count} {label}{'' if count == 1 or label.endswith('s') else 's'}")
    return ", ".join(parts)


# --- The guard ---------------------------------------------------------------------------------


@dataclass
class GuardOutcome:
    """What the guard did before a node ran (recorded on the step; no values)."""

    mode: str
    action: Literal["clean", "warned", "redacted", "blocked", "off"]
    findings: list[Finding] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {"mode": self.mode, "action": self.action, **summarize(self.findings),
                "fields": sorted({f.path.split(".")[0].split("[")[0] for f in self.findings})}


def guard(
    config: dict[str, Any], fields: Iterable[str], policy: PrivacyPolicy | None = None
) -> tuple[dict[str, Any], GuardOutcome]:
    """Apply a node's `privacy_guard` to the content `fields` of its resolved config.

    Returns the config to run with (redacted in "redact" mode) and what happened. The
    caller fails the node when the action is "blocked"."""
    policy = policy or PrivacyPolicy()
    mode = str(config.get("privacy_guard") or "off")
    if mode == "off":
        return config, GuardOutcome(mode, "off")
    content = {name: config[name] for name in fields if name in config and config[name] not in (None, "")}
    findings = scan(content, categories=policy.categories, allowlist=policy.allowlist)
    if not findings:
        return config, GuardOutcome(mode, "clean")
    if mode == "block":
        return config, GuardOutcome(mode, "blocked", findings)
    if mode == "warn":
        return config, GuardOutcome(mode, "warned", findings)
    return {**config, **redact(content, findings)}, GuardOutcome(mode, "redacted", findings)


def blocked_message(outcome: GuardOutcome) -> str:
    fields = ", ".join(outcome.report()["fields"])
    return (
        f"Blocked by the privacy guard: found {describe(outcome.findings)} in {fields}. Remove it, or set this "
        "node's privacy_guard to redact (send it masked) or warn (send it and record a warning)."
    )


# --- Reversible redaction (PII Redact / Restore) ---------------------------------------------


def pseudonymize(text: str, findings: list[Finding]) -> tuple[str, dict[str, str]]:
    """Each distinct value replaced by "<TYPE_n>" (the same value gets the same placeholder);
    returns the text and {placeholder: original}."""
    mapping: dict[str, str] = {}
    seen: dict[tuple[str, str], str] = {}
    numbers: Counter[str] = Counter()

    def placeholder(finding: Finding, original: str) -> str:
        key = (finding.type, original)
        if key not in seen:
            numbers[finding.type] += 1
            seen[key] = f"<{finding.type}_{numbers[finding.type]}>"
            mapping[seen[key]] = original
        return seen[key]

    return _replace(text, findings, placeholder), mapping


_PLACEHOLDER = re.compile(r"<([A-Z_]+_\d+)>")


def restore(text: str, mapping: dict[str, str]) -> tuple[str, int, list[str]]:
    """(text with placeholders put back, how many were replaced, placeholders in the text
    the mapping doesn't know)."""
    replaced, unknown = 0, []

    def put_back(match: re.Match[str]) -> str:
        nonlocal replaced
        if match.group(0) in mapping:
            replaced += 1
            return mapping[match.group(0)]
        unknown.append(match.group(0))
        return match.group(0)

    return _PLACEHOLDER.sub(put_back, text), replaced, sorted(set(unknown))


@runtime_checkable
class SecretVault(Protocol):
    """Short-lived storage for redaction mappings (encrypted Redis in the API, so a run that
    moves between queues and workers can still restore)."""

    async def put(self, data: dict[str, str]) -> str:
        """Store `data`; returns a reference to it."""
        ...

    async def get(self, ref: str) -> dict[str, str] | None:
        """The stored data, or None if it expired or doesn't exist."""
        ...


class MemoryVault:
    """In-process vault (tests, one-off runs)."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}

    async def put(self, data: dict[str, str]) -> str:
        ref = f"mem-{len(self._data) + 1}"
        self._data[ref] = dict(data)
        return ref

    async def get(self, ref: str) -> dict[str, str] | None:
        return dict(self._data[ref]) if ref in self._data else None
