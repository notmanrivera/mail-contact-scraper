import io
import json
import random
import shutil
from types import SimpleNamespace

import pytest

from mail_contact_scraper import cli, ocr, vision
from mail_contact_scraper.contacts import ContactBook, same_address
from mail_contact_scraper.extract import parse_signature


def _banner():
    """White text over a noisy dark 'photo', like a signature pasted as a banner."""
    from PIL import Image, ImageDraw, ImageFont

    rng = random.Random(4)
    img = Image.new("RGB", (520, 200), (40, 40, 48))
    draw = ImageDraw.Draw(img)
    for _ in range(400):  # the photo: blotches and lines Tesseract reads as letters
        x, y = rng.randrange(0, 200), rng.randrange(0, 200)
        draw.rectangle([x, y, x + rng.randrange(3, 25), y + rng.randrange(3, 25)],
                       fill=tuple(rng.randrange(60, 220) for _ in range(3)))
    font = ImageFont.load_default(size=22)
    for i, line in enumerate(["Atte. Mario A. Loera", "(442) 254 68 51", "m.alberto@piesamb.com"]):
        draw.text((215, 30 + i * 50), line, fill="white", font=font)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


@pytest.mark.skipif(not shutil.which("tesseract"), reason="needs Tesseract")
def test_reads_light_text_over_photo():
    reader = ocr.SignatureOCR()
    text, quality = reader._ocr(_banner())
    assert "Mario A. Loera" in text and "(442) 254 68 51" in text and "m.alberto@piesamb.com" in text
    assert 0 < quality <= 1
    contact = parse_signature(text, "", "m.alberto@piesamb.com")
    assert contact["name"] == "Mario A. Loera"
    assert [p[0] for p in contact["phones"]] == ["+524422546851"]


def test_low_confidence_words_and_icon_bits_are_dropped():
    data = {
        "text": ["»", "Atte.", "Mario", "Loera", "q442)", "2957070", "Ny", "ve", "@m.alberto@x.com"],
        "conf": [80, 96, 96, 96, 17, 94, 58, 40, 86],
        "block_num": [1, 1, 1, 1, 2, 2, 3, 3, 4],
        "par_num": [1] * 9,
        "line_num": [1] * 9,
        "top": [10, 10, 10, 10, 40, 40, 70, 70, 100],
    }
    lines, chars = ocr._lines(data)
    assert [l for _, l in lines] == ["Atte. Mario Loera", "(442) 2957070", "m.alberto@x.com"]
    assert chars > 0


def test_second_pass_adds_only_new_lines():
    kept = [(10, "Atte. Mario A. Loera"), (40, "(442) 295 70 70"), (90, "piesamb.com")]
    ocr._merge_line(kept, 12, "5 Atte. Mario A. Loera")      # same line plus icon noise
    ocr._merge_line(kept, 41, "(642)2957070")                 # same number, misread area code
    ocr._merge_line(kept, 91, "malberto@piesamb.com")         # longer, with a real word
    ocr._merge_line(kept, 60, "SUC. QUERETARO")
    assert [l for _, l in sorted(kept)] == [
        "Atte. Mario A. Loera", "(442) 295 70 70", "SUC. QUERETARO", "malberto@piesamb.com"]


def test_signoff_junk_company_and_labeled_company():
    c = parse_signature("Atte. Mario A. Loera\nSUC. QUERÉTARO\n< Y MA PIE SA\n(442) 295 70 70", "",
                        "m.alberto@piesamb.com")
    assert c["name"] == "Mario A. Loera" and c["company"] == "Piesamb"
    c = parse_signature("Saludos cordiales,\nLaura Martínez\nGerente | Ventas\nAceros del Norte S.A. de C.V.")
    assert (c["name"], c["title"], c["company"]) == ("Laura Martínez", "Gerente | Ventas", "Aceros del Norte S.A. de C.V.")
    assert parse_signature("Laura Martínez\nEmpresa: PIESA")["company"] == "PIESA"


def test_ocr_email_without_dot_is_the_sender():
    assert same_address("malberto@piesamb.com", "m.alberto@piesamb.com")
    assert not same_address("agarcia@x.com", "jgarcia@x.com")
    assert not same_address("m.alberto@otro.com", "m.alberto@piesamb.com")
    book = ContactBook()
    book.add_sender({"from_email": "m.alberto@piesamb.com", "signature": "x", "date": "2025-01-01",
                     "contact": {"name": "", "title": "", "company": "", "phones": [], "websites": [],
                                 "emails": ["malberto@piesamb.com", "ventas@piesamb.com"]}})
    assert book.to_rows()[0]["other_emails"] == "ventas@piesamb.com"


class FakeClient:
    def __init__(self, answer, stop_reason="end_turn"):
        self.answer, self.stop_reason, self.requests = answer, stop_reason, []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.answer))])


ANSWER = {"name": "Mario A. Loera", "title": "", "company": "PIESA", "website": "", "address": "Querétaro",
          "phones": [{"number": "(442) 295 70 70", "type": "office"}, {"number": "442 254 6851", "type": "mobile"}],
          "emails": ["m.alberto@piesamb.com"]}


def _vision(tmp_path, mode="auto", client=None):
    v = vision.ClaudeVision.__new__(vision.ClaudeVision)
    v.mode, v.model, v.calls = mode, vision.DEFAULT_MODEL, 0
    v.cache_path = tmp_path / "ocr_claude.json"
    v.cache = vision._load(v.cache_path)
    v.client = client or FakeClient(ANSWER)
    return v


def test_claude_reads_when_tesseract_is_unsure(tmp_path, monkeypatch):
    v = _vision(tmp_path)
    reader = ocr.SignatureOCR(vision=v)
    reader.enabled = True
    monkeypatch.setattr(reader, "_ocr", lambda data: ("< Y MA PIE SA", 0.3))
    text = reader.read(_banner())
    contact = parse_signature(text, "", "m.alberto@piesamb.com")
    assert contact["name"] == "Mario A. Loera" and contact["company"] == "PIESA"
    assert sorted(p[:2] for p in contact["phones"]) == [("+524422546851", "mobile"), ("+524422957070", "office")]
    request = v.client.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["messages"][0]["content"][0]["source"]["media_type"] == "image/png"

    # Same image in a later scan: answered from the cache file, no second call.
    again = _vision(tmp_path)
    assert again.read(next(iter(again.cache)), b"") == text and again.client.requests == []


def test_claude_is_not_asked_when_tesseract_is_sure(tmp_path, monkeypatch):
    v = _vision(tmp_path)
    reader = ocr.SignatureOCR(vision=v)
    reader.enabled = True
    monkeypatch.setattr(reader, "_ocr", lambda data: ("Laura Martínez", 0.95))
    assert reader.read(_banner()) == "Laura Martínez" and v.client.requests == []
    assert _vision(tmp_path, mode="claude").wanted(0.95)


def test_claude_refusal_keeps_tesseract_text(tmp_path, monkeypatch):
    v = _vision(tmp_path, client=FakeClient({}, stop_reason="refusal"))
    reader = ocr.SignatureOCR(vision=v)
    reader.enabled = True
    monkeypatch.setattr(reader, "_ocr", lambda data: ("Mario Loera", 0.4))
    assert reader.read(_banner()) == "Mario Loera" and not v.cache


def test_engine_needs_a_key(tmp_path):
    from mail_contact_scraper.config import Settings
    assert vision.from_settings(Settings(ocr_engine="auto"), tmp_path) is None
    assert vision.from_settings(Settings(ocr_engine="tesseract", anthropic_api_key="k"), tmp_path) is None


def test_reocr_forgets_image_signatures(tmp_path):
    path = tmp_path / "messages.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in [
        {"uid": 1, "signature_from_image": True}, {"uid": 2, "signature_from_image": False}, {"uid": 3}]))
    assert cli._forget_image_signatures(path) == 1
    assert [json.loads(l)["uid"] for l in path.read_text().splitlines()] == [2, 3]


def test_dashboard_saves_ocr_engine_without_showing_the_key(tmp_path):
    from mail_contact_scraper import dashboard, envfile
    env = tmp_path / ".env"
    env.write_text("IMAP_USER=pedro@dindustrimex.com\n")
    code, payload = dashboard.settings_action("/settings/ocr", {"engine": "auto"}, tmp_path, env)
    assert code == 400 and "clave" in payload["error"]
    code, payload = dashboard.settings_action("/settings/ocr", {"engine": "auto", "api_key": "sk-ant-abc"}, tmp_path, env)
    assert code == 200 and payload["ocr"]["engine"] == "auto" and payload["ocr"]["has_key"]
    assert "sk-ant-abc" not in json.dumps(payload)
    code, _ = dashboard.settings_action("/settings/ocr", {"engine": "tesseract"}, tmp_path, env)
    assert envfile.read(env) == {"IMAP_USER": "pedro@dindustrimex.com", "OCR_ENGINE": "tesseract",
                                 "ANTHROPIC_API_KEY": "sk-ant-abc"}
    assert dashboard.settings_action("/settings/ocr", {"engine": "gpt"}, tmp_path, env)[0] == 400
