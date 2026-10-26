"""Read and update the local .env without losing its comments or layout."""
import os
import re
from pathlib import Path

LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return re.sub(r"\s+#.*$", "", value)  # trailing comment on an unquoted value


def _quote(value):
    value = str(value)
    if value and not re.search(r"[\s#\"'\\=]", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read(path=".env"):
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        match = LINE_RE.match(line)
        if match:
            raw = match.group(2).strip()
            if raw[:1] == '"' and raw.endswith('"') and len(raw) >= 2:
                values[match.group(1)] = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            else:
                values[match.group(1)] = _unquote(raw)
    return values


def environ(path=".env", base=None):
    """The process environment over the .env values: a variable set in the shell wins."""
    return {**read(path), **(os.environ if base is None else base)}


def update(path=".env", changes=None):
    """Set keys to values (None removes the key). Edits lines in place, appends new keys."""
    changes = dict(changes or {})
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    out = []
    for line in lines:
        match = LINE_RE.match(line)
        key = match.group(1) if match else None
        if key in changes:
            value = changes.pop(key)
            if value is not None:
                out.append(f"{key}={_quote(value)}")
            continue
        out.append(line)
    new = [f"{k}={_quote(v)}" for k, v in changes.items() if v is not None]
    if new:
        if out and out[-1].strip():
            out.append("")
        out.extend(new)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)  # it holds passwords
    os.replace(tmp, path)
