"""pytest suite for edi835_validator. Run: pytest -q"""
from pathlib import Path

import pytest

from edi835_validator import Validator835, validate_file

SAMPLES = Path(__file__).parent / "samples"


def rules(v):
    return {f.rule for f in v.findings if f.severity == "ERROR"}


def test_valid_file_has_no_findings():
    v = validate_file(SAMPLES / "valid_835.edi")
    assert v.findings == []
    assert v.stats["claims"] == 3
    assert str(v.stats["total_paid"]) == "295"


@pytest.mark.parametrize("rule", [
    "TRANSACTION_BALANCE", "SERVICE_BALANCE", "CLAIM_BALANCE",
    "INVALID_CODE", "DATE_FORMAT", "COUNT", "CONTROL_NUMBER",
])
def test_invalid_file_catches(rule):
    v = validate_file(SAMPLES / "invalid_835.edi")
    assert rule in rules(v)


def test_not_x12():
    v = Validator835("x", "hello world").run()
    assert rules(v) == {"PARSE"}


def test_missing_trailers():
    text = (SAMPLES / "valid_835.edi").read_text()
    truncated = text.split("SE*")[0]
    v = Validator835("x", truncated).run()
    msgs = " ".join(f.message for f in v.findings)
    assert "Missing SE" in msgs and "Missing GE" in msgs and "Missing IEA" in msgs


def test_tolerance_allows_rounding():
    text = (SAMPLES / "valid_835.edi").read_text().replace("BPR*I*285.00", "BPR*I*285.01")
    assert "TRANSACTION_BALANCE" in rules(Validator835("x", text).run())
    assert "TRANSACTION_BALANCE" not in rules(Validator835("x", text, tolerance=__import__("decimal").Decimal("0.01")).run())
