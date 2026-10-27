import pytest


@pytest.fixture(autouse=True)
def _no_website_visits(monkeypatch):
    # A scan visits each company's website at the end; tests never go online.
    monkeypatch.setenv("ENRICH_DOMAINS", "false")
