"""Optional second reader for image signatures: Claude (Anthropic API) reads the ones
Tesseract can't, such as white text over a photo.

Off by default, because it sends the signature image to Anthropic. Turn it on with
OCR_ENGINE=auto (only when Tesseract is unsure) or OCR_ENGINE=claude (every image),
plus ANTHROPIC_API_KEY in .env and `pip install anthropic`.
"""
import base64
import io
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
LOW_QUALITY = 0.75      # share of the image's text Tesseract trusted; below this, ask Claude
MAX_SIDE = 1568         # bigger images are resized by the API anyway

PROMPT = (
    "This image is the signature block of a business email. Transcribe the contact details "
    "exactly as written. Leave a field empty when it is not in the image; never guess. "
    "Ignore slogans, legal notices and text that is part of a photo or product."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "title": {"type": "string"},
        "company": {"type": "string"},
        "phones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "string"},
                    "type": {"type": "string", "enum": ["mobile", "office", "fax"]},
                },
                "required": ["number", "type"],
                "additionalProperties": False,
            },
        },
        "emails": {"type": "array", "items": {"type": "string"}},
        "website": {"type": "string"},
        "address": {"type": "string"},
    },
    "required": ["name", "title", "company", "phones", "emails", "website", "address"],
    "additionalProperties": False,
}
PHONE_LABELS = {"mobile": "Cel.", "office": "Tel.", "fax": "Fax"}


class ClaudeVision:
    def __init__(self, api_key, mode="auto", model=DEFAULT_MODEL, cache_path=None):
        self.mode = mode
        self.model = model or DEFAULT_MODEL
        self.cache_path = Path(cache_path) if cache_path else None
        self.cache = _load(self.cache_path)
        self.calls = 0
        self.client = None
        try:
            import anthropic
        except ImportError:
            log.warning("Claude OCR disabled: pip install anthropic")
            return
        self.client = anthropic.Anthropic(api_key=api_key)

    def wanted(self, quality):
        if not self.client:
            return False
        return self.mode == "claude" or (quality is not None and quality < LOW_QUALITY)

    def read(self, key, data):
        """Signature text in the layout parse_signature() expects, or "" if it failed."""
        if key in self.cache:
            return self.cache[key]
        try:
            fields = self._ask(data)
        except Exception as exc:  # network, quota, bad key: keep Tesseract's text
            log.warning("Claude could not read a signature image: %s", exc)
            return ""
        if fields is None:
            return ""
        text = to_text(fields)
        self.cache[key] = text
        _save(self.cache_path, self.cache)
        return text

    def _ask(self, data):
        self.calls += 1
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png(data)}},
                {"type": "text", "text": PROMPT},
            ]}],
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            log.warning("Claude did not read a signature image (%s)", response.stop_reason)
            return None
        return json.loads(next(b.text for b in response.content if b.type == "text"))


def to_text(fields):
    """One item per line, labeled so parse_signature() and find_phones() pick them up."""
    lines = [fields.get("name", ""), fields.get("title", "")]
    if fields.get("company"):
        lines.append(f"Empresa: {fields['company']}")
    for phone in fields.get("phones") or []:
        lines.append(f"{PHONE_LABELS.get(phone.get('type'), 'Tel.')} {phone.get('number', '')}")
    lines += fields.get("emails") or []
    lines += [fields.get("website", ""), fields.get("address", "")]
    return "\n".join(l.strip() for l in lines if l and l.strip())


def _png(data):
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img.seek(0)
    img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "PNG")
    return base64.standard_b64encode(out.getvalue()).decode()


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}


def _save(path, cache):
    # Kept next to the contacts so a rescan never pays twice for the same image.
    if not path:
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def from_settings(settings, out_dir):
    if settings.ocr_engine not in ("auto", "claude"):
        return None
    if not settings.anthropic_api_key:
        log.warning("OCR_ENGINE=%s needs ANTHROPIC_API_KEY in .env; using Tesseract only", settings.ocr_engine)
        return None
    return ClaudeVision(settings.anthropic_api_key, settings.ocr_engine, settings.ocr_claude_model,
                        Path(out_dir) / "ocr_claude.json")


if __name__ == "__main__":  # the Mac app's build checks that the SDK made it into the bundle
    import anthropic

    anthropic.Anthropic(api_key="sk-ant-check")
    print("anthropic", anthropic.__version__)
