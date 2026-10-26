"""Isolate the sender's signature from a message body.

Heuristics, in order:
  1. Cut everything from the first quoted reply / forwarded block on.
  2. Start the signature at a "-- " delimiter, or right after a sign-off line
     ("Saludos", "Best regards", ...), or at the line holding the sender's name.
  3. Otherwise take the last few lines if they look like contact details.
  4. Drop legal disclaimers and "Sent from my iPhone" lines.
"""
import re
import unicodedata

QUOTE_MARKERS = [
    r"^\[\[QUOTE\]\]$",
    r"^-{2,}\s*(original message|mensaje original|forwarded message|mensaje reenviado)\s*-{2,}",
    r"^_{5,}$",
    r"^on .{5,200}wrote:\s*$",
    r"^el .{5,200}escribi[oó]:\s*$",
    r"^(from|de)\s*:\s.+@",
    r"^(from|de)\s*:\s.+$(?=\n\s*(sent|enviado|date|fecha)\s*:)",
    r"^>",
]
QUOTE_RE = re.compile("|".join(QUOTE_MARKERS), re.IGNORECASE | re.MULTILINE)

SIGN_OFFS = [
    "saludos", "saludos cordiales", "un saludo", "atentamente", "cordialmente",
    "quedo a sus ordenes", "quedo a tus ordenes", "quedo atento", "quedo atenta",
    "quedo pendiente", "gracias", "muchas gracias", "mil gracias", "sin mas por el momento",
    "best regards", "kind regards", "warm regards", "regards", "best", "thanks",
    "thank you", "many thanks", "sincerely", "cheers", "respectfully", "br",
]

NOISE_RE = re.compile(
    r"(sent from my|enviado desde mi|get outlook for|obtener outlook para|"
    r"confidencial|confidential|aviso de privacidad|privacy notice|disclaimer|"
    r"this e-?mail|este correo|antes de imprimir|please consider the environment)",
    re.IGNORECASE,
)
IMG_TOKEN_RE = re.compile(r"\[\[IMG:(\d+)\]\]")
CONTACT_HINT_RE = re.compile(r"(\[\[IMG:|@|\+?\d[\d\s().-]{6,}\d|www\.|https?://|tel|cel|phone|m[oó]vil)", re.I)

MAX_SIG_LINES = 15


def _fold(text):
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def strip_quoted(body):
    match = QUOTE_RE.search(body)
    return body[: match.start()] if match else body


def _is_sign_off(line):
    folded = re.sub(r"[^a-z ]", "", _fold(line)).strip()
    if not folded or len(folded) > 40:
        return False
    words = folded.split()
    for s in SIGN_OFFS:
        # "Saludos cordiales," or "Gracias de antemano" but not "Gracias por tu correo de ayer"
        if folded == s or (folded.startswith(s + " ") and len(words) <= len(s.split()) + 2):
            return True
    return False


def _clean(lines):
    out = []
    for line in lines:
        line = line.strip().strip("|").strip()
        if not line:
            continue
        if NOISE_RE.search(line) or len(line) > 160:
            # Disclaimers are long and come last; stop once one starts.
            if out:
                break
            continue
        out.append(line)
    return out[:MAX_SIG_LINES]


def extract_signature(body, sender_name=""):
    text = strip_quoted(body or "").rstrip()
    lines = text.splitlines()
    if not lines:
        return ""

    start = None
    # 1. Standard "-- " delimiter (last one wins).
    for i, line in enumerate(lines):
        if line.rstrip() in ("--", "-- ", "—", "__"):
            start = i + 1
    # 2. Sign-off line, searched from the bottom up within the last 25 lines.
    if start is None:
        window = range(len(lines) - 1, max(-1, len(lines) - 26), -1)
        for i in window:
            if _is_sign_off(lines[i]):
                start = i + 1
                break
    # 3. Sender's name on its own line near the end.
    if start is None and sender_name:
        first = _fold(sender_name.split()[0]) if sender_name.split() else ""
        for i in range(len(lines) - 1, max(-1, len(lines) - 20), -1):
            folded = _fold(lines[i])
            if first and folded.startswith(first) and len(lines[i]) < 60:
                start = i
                break
    # 4. Trailing block with contact-looking lines.
    if start is None:
        tail = lines[-12:]
        if sum(1 for l in tail if CONTACT_HINT_RE.search(l)) >= 1:
            start = max(1, len(lines) - len(tail))
            # Walk back to the blank line that separates the block.
            for i in range(len(lines) - 1, start - 1, -1):
                if not lines[i].strip() and any(CONTACT_HINT_RE.search(l) for l in lines[i:]):
                    start = i + 1
                    break
    if start is None or start >= len(lines):
        return ""
    return "\n".join(_clean(lines[start:]))
