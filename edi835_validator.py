#!/usr/bin/env python3
"""
edi835_validator.py - Validate X12 835 (Health Care Claim Payment/Advice, 005010X221A1) files.

What it checks
--------------
Envelope    ISA/IEA, GS/GE, ST/SE pairing; matching control numbers; IEA01/GE01/SE01 counts;
            ISA fixed width (106 chars); GS01 = HP; GS08 = 005010X221*
Structure   BPR first after ST; required TRN, DTM*405, N1*PR, N1*PE; NM1*QC per claim;
            segments outside a transaction; unknown segment tags
Code values BPR01/03/04, TRN01, CLP02, CLP06, CAS01, PLB adjustment reason codes
Formats     monetary amounts, CCYYMMDD dates
Balancing   Service line : SVC02 - sum(service CAS)                == SVC03
            Claim (lines): sum(SVC03) - sum(claim CAS)              == CLP04
            Claim (none) : CLP03 - sum(claim CAS)                   == CLP04
            Transaction  : sum(CLP04) - sum(PLB amounts)            == BPR02
            Warnings     : sum(SVC02) vs CLP03, CLP05 vs PR adjustments,
                           denied claim (CLP02=4) with payment, duplicate CLP01

Usage
-----
  python edi835_validator.py FILE_OR_DIR [FILE_OR_DIR ...]
         [--csv findings.csv] [--json findings.json] [--claims-csv claims.csv]
         [--tolerance 0.00] [--strict] [--quiet]

Exit codes: 0 = no errors (warnings allowed unless --strict), 1 = errors found, 2 = unreadable input.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# --------------------------------------------------------------------------- code sets
KNOWN_TAGS = {
    "ISA", "IEA", "GS", "GE", "ST", "SE", "BPR", "TRN", "CUR", "REF", "DTM", "N1", "N2",
    "N3", "N4", "PER", "RDM", "LX", "TS3", "TS2", "CLP", "CAS", "NM1", "MIA", "MOA",
    "AMT", "QTY", "SVC", "LQ", "PLB",
}
BPR01_CODES = {"C", "D", "H", "I", "P", "U", "X"}
BPR03_CODES = {"C", "D"}
BPR04_CODES = {"ACH", "BOP", "CHK", "FWT", "NON"}
CLP02_CODES = {"1", "2", "3", "4", "19", "20", "21", "22", "23", "25"}
CLP06_CODES = {"12", "13", "14", "15", "16", "17", "AM", "CH", "DS", "HM", "LM", "MA",
               "MB", "MC", "OF", "TV", "VA", "WC", "ZZ"}
CAS01_CODES = {"CO", "CR", "OA", "PI", "PR"}
PLB_CODES = {"50", "51", "72", "90", "AH", "AM", "AP", "B2", "B3", "BD", "BN", "C5", "CR",
             "CS", "CT", "CV", "CW", "DM", "E3", "FB", "FC", "GO", "HM", "IP", "IR", "IS",
             "J1", "L3", "L6", "LE", "LS", "OA", "OB", "PI", "PL", "RA", "RE", "SL", "TL",
             "WO", "WU"}
SVC_QUALIFIERS = {"AD", "ER", "HC", "HP", "IV", "N4", "NU", "WK"}

AMOUNT_RE = re.compile(r"^-?(\d+(\.\d*)?|\.\d+)$")
ZERO = Decimal("0")


# --------------------------------------------------------------------------- data model
@dataclass
class Segment:
    tag: str
    elements: list[str]          # elements[0] is the tag, so elements[1] == XX01
    index: int                   # 1-based position in the file
    raw: str

    def get(self, n: int) -> str:
        return self.elements[n].strip() if n < len(self.elements) else ""


@dataclass
class Finding:
    file: str
    severity: str                # ERROR | WARNING
    rule: str
    message: str
    segment_index: int | None = None
    segment: str = ""
    claim_id: str = ""


@dataclass
class Adjustment:
    group: str
    reason: str
    amount: Decimal


@dataclass
class ServiceLine:
    seg: Segment
    code: str
    charge: Decimal | None
    paid: Decimal | None
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class Claim:
    seg: Segment
    claim_id: str
    status: str
    charge: Decimal | None
    paid: Decimal | None
    patient_resp: Decimal | None
    payer_control: str
    adjustments: list[Adjustment] = field(default_factory=list)
    services: list[ServiceLine] = field(default_factory=list)
    has_patient: bool = False
    interest: Decimal | None = None

    def all_adjustments(self) -> list[Adjustment]:
        return self.adjustments + [a for s in self.services for a in s.adjustments]


# --------------------------------------------------------------------------- parsing
class Delimiters:
    def __init__(self, element: str, component: str, segment: str, repetition: str):
        self.element, self.component, self.segment, self.repetition = (
            element, component, segment, repetition)


def parse_x12(text: str) -> tuple[list[Segment], Delimiters]:
    text = text.lstrip("﻿ \t\r\n")
    if not text.startswith("ISA") or len(text) < 106:
        raise ValueError("File does not start with a valid ISA segment (need 106 characters).")
    elem, comp, seg_term = text[3], text[104], text[105]
    rep = text[82] if text[82] not in (elem,) else ""
    segments: list[Segment] = []
    for chunk in text.split(seg_term):
        raw = chunk.strip("\r\n")
        if not raw.strip():
            continue
        parts = raw.split(elem)
        segments.append(Segment(parts[0].strip().upper(), parts, len(segments) + 1, raw))
    return segments, Delimiters(elem, comp, seg_term, rep)


def valid_date(value: str, fmt: str = "%Y%m%d") -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- validator
class Validator835:
    def __init__(self, file_label: str, text: str, tolerance: Decimal = ZERO):
        self.file = file_label
        self.text = text
        self.tol = tolerance
        self.findings: list[Finding] = []
        self.claim_rows: list[dict] = []
        self.stats = {"segments": 0, "transactions": 0, "claims": 0, "total_paid": ZERO}
        self.delims: Delimiters | None = None

    # ---- helpers
    def _add(self, sev, rule, msg, seg: Segment | None = None, claim_id=""):
        self.findings.append(Finding(
            self.file, sev, rule, msg,
            seg.index if seg else None, seg.raw if seg else "", claim_id))

    def err(self, rule, msg, seg=None, claim_id=""):
        self._add("ERROR", rule, msg, seg, claim_id)

    def warn(self, rule, msg, seg=None, claim_id=""):
        self._add("WARNING", rule, msg, seg, claim_id)

    def amount(self, seg: Segment, pos: int, label: str, required=True, claim_id="") -> Decimal | None:
        raw = seg.get(pos)
        if raw == "":
            if required:
                self.err("REQUIRED_ELEMENT", f"{label} ({seg.tag}{pos:02d}) is missing", seg, claim_id)
            return None
        if not AMOUNT_RE.match(raw):
            self.err("AMOUNT_FORMAT", f"{label} ({seg.tag}{pos:02d}) '{raw}' is not a valid amount",
                     seg, claim_id)
            return None
        return Decimal(raw)

    def required(self, seg: Segment, pos: int, label: str, claim_id="") -> str:
        val = seg.get(pos)
        if val == "":
            self.err("REQUIRED_ELEMENT", f"{label} ({seg.tag}{pos:02d}) is missing", seg, claim_id)
        return val

    def code(self, seg: Segment, pos: int, label: str, allowed: set[str], claim_id="", required=True):
        val = seg.get(pos)
        if val == "":
            if required:
                self.err("REQUIRED_ELEMENT", f"{label} ({seg.tag}{pos:02d}) is missing", seg, claim_id)
        elif val not in allowed:
            self.err("INVALID_CODE", f"{label} ({seg.tag}{pos:02d}) '{val}' is not a valid code",
                     seg, claim_id)
        return val

    def off(self, a: Decimal, b: Decimal) -> bool:
        return abs(a - b) > self.tol

    # ---- entry point
    def run(self) -> "Validator835":
        try:
            segments, self.delims = parse_x12(self.text)
        except ValueError as exc:
            self.err("PARSE", str(exc))
            return self
        self.stats["segments"] = len(segments)
        self._walk_envelopes(segments)
        return self

    # ---- envelope
    def _walk_envelopes(self, segments: list[Segment]):
        isa = gs = None
        txn: list[Segment] | None = None
        gs_count = st_count = 0

        if segments[0].tag != "ISA":
            self.err("ENVELOPE", "First segment must be ISA", segments[0])

        for s in segments:
            t = s.tag
            if t == "ISA":
                if isa:
                    self.err("ENVELOPE", "ISA found before previous IEA", s)
                isa, gs_count = s, 0
                self._check_isa(s)
            elif t == "IEA":
                if not isa:
                    self.err("ENVELOPE", "IEA without matching ISA", s)
                    continue
                if s.get(1) != str(gs_count):
                    self.err("COUNT", f"IEA01 says {s.get(1)} groups, found {gs_count}", s)
                if s.get(2) != isa.get(13):
                    self.err("CONTROL_NUMBER", f"IEA02 '{s.get(2)}' does not match ISA13 '{isa.get(13)}'", s)
                isa = None
            elif t == "GS":
                if gs:
                    self.err("ENVELOPE", "GS found before previous GE", s)
                if not isa:
                    self.err("ENVELOPE", "GS outside ISA/IEA", s)
                gs, st_count = s, 0
                gs_count += 1
                if s.get(1) != "HP":
                    self.err("INVALID_CODE", f"GS01 '{s.get(1)}' should be 'HP' for an 835", s)
                if not s.get(8).startswith("005010X221"):
                    self.warn("VERSION", f"GS08 '{s.get(8)}' is not 005010X221/005010X221A1", s)
                if s.get(4) and not valid_date(s.get(4)):
                    self.err("DATE_FORMAT", f"GS04 '{s.get(4)}' is not CCYYMMDD", s)
            elif t == "GE":
                if not gs:
                    self.err("ENVELOPE", "GE without matching GS", s)
                    continue
                if s.get(1) != str(st_count):
                    self.err("COUNT", f"GE01 says {s.get(1)} transactions, found {st_count}", s)
                if s.get(2) != gs.get(6):
                    self.err("CONTROL_NUMBER", f"GE02 '{s.get(2)}' does not match GS06 '{gs.get(6)}'", s)
                gs = None
            elif t == "ST":
                if txn is not None:
                    self.err("ENVELOPE", "ST found before previous SE", s)
                if not gs:
                    self.err("ENVELOPE", "ST outside GS/GE", s)
                st_count += 1
                txn = [s]
            elif t == "SE":
                if txn is None:
                    self.err("ENVELOPE", "SE without matching ST", s)
                    continue
                txn.append(s)
                self._check_transaction(txn)
                txn = None
            else:
                if txn is None:
                    self.err("ENVELOPE", f"{t} segment is outside an ST/SE transaction", s)
                else:
                    txn.append(s)

        if txn is not None:
            self.err("ENVELOPE", "Missing SE: transaction not closed", txn[0])
        if gs:
            self.err("ENVELOPE", "Missing GE: functional group not closed", gs)
        if isa:
            self.err("ENVELOPE", "Missing IEA: interchange not closed", isa)

    def _check_isa(self, s: Segment):
        if len(s.elements) != 17:
            self.err("ISA_FORMAT", f"ISA has {len(s.elements) - 1} elements, expected 16", s)
            return
        if len(s.raw) != 105:
            self.err("ISA_FORMAT", f"ISA must be fixed width (106 chars incl. terminator), got {len(s.raw) + 1}", s)
        if not re.fullmatch(r"\d{9}", s.get(13)):
            self.err("ISA_FORMAT", f"ISA13 control number '{s.get(13)}' must be 9 digits", s)
        if not valid_date(s.get(9), "%y%m%d"):
            self.err("DATE_FORMAT", f"ISA09 '{s.get(9)}' is not YYMMDD", s)
        if not valid_date(s.get(10), "%H%M"):
            self.err("DATE_FORMAT", f"ISA10 '{s.get(10)}' is not HHMM", s)
        if s.get(12) != "00501":
            self.warn("VERSION", f"ISA12 '{s.get(12)}' is not 00501", s)
        if s.get(15) not in {"P", "T"}:
            self.err("INVALID_CODE", f"ISA15 '{s.get(15)}' must be P or T", s)

    # ---- transaction
    def _check_transaction(self, segs: list[Segment]):
        st, se = segs[0], segs[-1]
        body = segs[1:-1]
        self.stats["transactions"] += 1
        st_ctrl = st.get(2)

        if st.get(1) != "835":
            self.err("INVALID_CODE", f"ST01 '{st.get(1)}' is not 835", st)
        if se.get(1) != str(len(segs)):
            self.err("COUNT", f"SE01 says {se.get(1)} segments, counted {len(segs)} (ST through SE)", se)
        if se.get(2) != st_ctrl:
            self.err("CONTROL_NUMBER", f"SE02 '{se.get(2)}' does not match ST02 '{st_ctrl}'", se)
        if not body or body[0].tag != "BPR":
            self.err("STRUCTURE", "BPR must be the first segment after ST", body[0] if body else st)

        bpr = trn = None
        has_405 = has_payer = has_payee = False
        claims: list[Claim] = []
        claim: Claim | None = None
        svc: ServiceLine | None = None
        plb_total = ZERO

        for s in body:
            t = s.tag
            cid = claim.claim_id if claim else ""
            if t not in KNOWN_TAGS:
                self.warn("UNKNOWN_SEGMENT", f"Segment '{t}' is not used in the 835", s, cid)

            if t == "BPR":
                bpr = s
                self._check_bpr(s)
            elif t == "TRN":
                trn = s
                self.code(s, 1, "Trace type code", {"1"})
                self.required(s, 2, "Check/EFT trace number")
                self.required(s, 3, "Payer identifier")
            elif t == "DTM":
                if s.get(2) and not valid_date(s.get(2)):
                    self.err("DATE_FORMAT", f"DTM02 '{s.get(2)}' is not a valid CCYYMMDD date", s, cid)
                if claim is None and s.get(1) == "405":
                    has_405 = True
            elif t == "N1" and claim is None:
                has_payer |= s.get(1) == "PR"
                has_payee |= s.get(1) == "PE"
            elif t == "LX":
                claim = svc = None
            elif t == "CLP":
                claim = self._start_claim(s)
                claims.append(claim)
                svc = None
            elif t == "NM1" and claim is not None and s.get(1) == "QC":
                claim.has_patient = True
            elif t == "AMT" and claim is not None and svc is None and s.get(1) == "I":
                claim.interest = self.amount(s, 2, "Interest amount", claim_id=cid)
            elif t == "SVC":
                if claim is None:
                    self.err("STRUCTURE", "SVC appears outside a CLP claim loop", s)
                else:
                    svc = self._start_service(s, claim)
            elif t == "CAS":
                target = svc if svc is not None else claim
                if target is None:
                    self.err("STRUCTURE", "CAS appears outside a claim/service loop", s)
                else:
                    target.adjustments.extend(self._parse_cas(s, cid))
            elif t == "PLB":
                claim = svc = None
                plb_total += self._parse_plb(s)

        if trn is None:
            self.err("REQUIRED_SEGMENT", f"Transaction {st_ctrl}: TRN (reassociation trace) is missing", st)
        if not has_405:
            self.err("REQUIRED_SEGMENT", f"Transaction {st_ctrl}: DTM*405 (production date) is missing", st)
        if not has_payer:
            self.err("REQUIRED_SEGMENT", f"Transaction {st_ctrl}: N1*PR (payer) loop is missing", st)
        if not has_payee:
            self.err("REQUIRED_SEGMENT", f"Transaction {st_ctrl}: N1*PE (payee) loop is missing", st)

        seen: dict[str, int] = {}
        for c in claims:
            self._check_claim(c)
            if c.claim_id in seen:
                self.warn("DUPLICATE_CLAIM", f"CLP01 '{c.claim_id}' also appears at segment {seen[c.claim_id]}",
                          c.seg, c.claim_id)
            seen.setdefault(c.claim_id, c.seg.index)
            self.claim_rows.append({
                "file": self.file, "st_control": st_ctrl, "trace_number": trn.get(2) if trn else "",
                "claim_id": c.claim_id, "status": c.status, "charge": c.charge, "paid": c.paid,
                "patient_resp": c.patient_resp, "payer_claim_control": c.payer_control,
                "service_lines": len(c.services),
                "total_adjustments": sum((a.amount for a in c.all_adjustments()), ZERO),
            })

        self.stats["claims"] += len(claims)
        paid_total = sum((c.paid for c in claims if c.paid is not None), ZERO)
        self.stats["total_paid"] += paid_total

        if bpr is not None:
            bpr02 = self._safe_dec(bpr.get(2))
            if bpr02 is not None:
                expected = paid_total - plb_total
                if self.off(bpr02, expected):
                    self.err("TRANSACTION_BALANCE",
                             f"BPR02 {bpr02} != sum(CLP04) {paid_total} - sum(PLB) {plb_total} = {expected}",
                             bpr)
                if bpr.get(1) == "H" and bpr02 != ZERO:
                    self.warn("BPR_NOTIFICATION", "BPR01 = H (notification only) but BPR02 is not zero", bpr)

    @staticmethod
    def _safe_dec(raw: str) -> Decimal | None:
        return Decimal(raw) if AMOUNT_RE.match(raw or "") else None

    def _check_bpr(self, s: Segment):
        self.code(s, 1, "Transaction handling code", BPR01_CODES)
        self.amount(s, 2, "Total payment amount")
        self.code(s, 3, "Credit/debit flag", BPR03_CODES)
        method = self.code(s, 4, "Payment method", BPR04_CODES)
        if method == "ACH":
            for pos, label in [(6, "Sender DFI qualifier"), (7, "Sender DFI number"),
                               (9, "Sender account number"), (12, "Receiver DFI qualifier"),
                               (13, "Receiver DFI number"), (15, "Receiver account number")]:
                self.required(s, pos, label)
        date = self.required(s, 16, "Payment effective date")
        if date and not valid_date(date):
            self.err("DATE_FORMAT", f"BPR16 '{date}' is not a valid CCYYMMDD date", s)

    def _start_claim(self, s: Segment) -> Claim:
        cid = self.required(s, 1, "Patient control number")
        self.code(s, 2, "Claim status code", CLP02_CODES, cid)
        self.code(s, 6, "Claim filing indicator", CLP06_CODES, cid)
        self.required(s, 7, "Payer claim control number", cid)
        return Claim(
            seg=s, claim_id=cid, status=s.get(2),
            charge=self.amount(s, 3, "Total claim charge", claim_id=cid),
            paid=self.amount(s, 4, "Claim payment amount", claim_id=cid),
            patient_resp=self.amount(s, 5, "Patient responsibility", required=False, claim_id=cid),
            payer_control=s.get(7),
        )

    def _start_service(self, s: Segment, claim: Claim) -> ServiceLine:
        cid = claim.claim_id
        comp = self.delims.component if self.delims else ":"
        proc = s.get(1).split(comp)
        if len(proc) < 2 or not proc[1]:
            self.err("REQUIRED_ELEMENT", f"SVC01 '{s.get(1)}' needs qualifier and procedure code", s, cid)
        elif proc[0] not in SVC_QUALIFIERS:
            self.err("INVALID_CODE", f"SVC01-1 qualifier '{proc[0]}' is not valid", s, cid)
        line = ServiceLine(
            seg=s, code=s.get(1),
            charge=self.amount(s, 2, "Line charge", claim_id=cid),
            paid=self.amount(s, 3, "Line payment", claim_id=cid),
        )
        claim.services.append(line)
        return line

    def _parse_cas(self, s: Segment, cid: str) -> list[Adjustment]:
        group = self.code(s, 1, "Claim adjustment group code", CAS01_CODES, cid)
        out = []
        for pos in range(2, 20, 3):           # up to 6 reason/amount/quantity triplets
            reason = s.get(pos)
            if not reason and not s.get(pos + 1):
                continue
            if not reason:
                self.err("REQUIRED_ELEMENT", f"CAS{pos:02d} reason code missing for amount", s, cid)
            amt = self.amount(s, pos + 1, f"Adjustment amount for reason {reason}", claim_id=cid)
            if amt is not None:
                out.append(Adjustment(group, reason, amt))
        if not out:
            self.err("REQUIRED_ELEMENT", "CAS has no reason/amount pairs", s, cid)
        return out

    def _parse_plb(self, s: Segment) -> Decimal:
        self.required(s, 1, "Provider identifier")
        date = self.required(s, 2, "Fiscal period date")
        if date and not valid_date(date):
            self.err("DATE_FORMAT", f"PLB02 '{date}' is not a valid CCYYMMDD date", s)
        comp = self.delims.component if self.delims else ":"
        total = ZERO
        for pos in range(3, 15, 2):            # up to 6 adjustment id/amount pairs
            ident = s.get(pos)
            if not ident and not s.get(pos + 1):
                continue
            reason = ident.split(comp)[0]
            if reason not in PLB_CODES:
                self.err("INVALID_CODE", f"PLB{pos:02d} adjustment reason '{reason}' is not valid", s)
            amt = self.amount(s, pos + 1, f"Provider adjustment amount ({reason})")
            if amt is not None:
                total += amt
        return total

    def _check_claim(self, c: Claim):
        cid = c.claim_id
        if not c.has_patient:
            self.err("REQUIRED_SEGMENT", "Claim has no NM1*QC (patient name) segment", c.seg, cid)

        for line in c.services:
            if line.charge is None or line.paid is None:
                continue
            adj = sum((a.amount for a in line.adjustments), ZERO)
            if self.off(line.charge - adj, line.paid):
                self.err("SERVICE_BALANCE",
                         f"{line.code}: SVC02 {line.charge} - CAS {adj} = {line.charge - adj}, "
                         f"but SVC03 is {line.paid}", line.seg, cid)

        if c.charge is None or c.paid is None:
            return
        claim_adj = sum((a.amount for a in c.adjustments), ZERO)

        if c.services:
            if any(l.paid is None or l.charge is None for l in c.services):
                return
            line_paid = sum((l.paid for l in c.services), ZERO)
            line_charge = sum((l.charge for l in c.services), ZERO)
            expected = line_paid - claim_adj
            formula = f"sum(SVC03) {line_paid} - claim CAS {claim_adj} = {expected}"
            if self.off(line_charge, c.charge):
                self.warn("CLAIM_CHARGE_MISMATCH",
                          f"sum(SVC02) {line_charge} != CLP03 {c.charge}", c.seg, cid)
        else:
            expected = c.charge - claim_adj
            formula = f"CLP03 {c.charge} - claim CAS {claim_adj} = {expected}"

        if self.off(c.paid, expected):
            if c.interest and not self.off(c.paid, expected + c.interest):
                self.warn("CLAIM_BALANCE_INTEREST",
                          f"CLP04 {c.paid} balances only when AMT*I interest {c.interest} is included",
                          c.seg, cid)
            else:
                self.err("CLAIM_BALANCE", f"CLP04 {c.paid} != {formula}", c.seg, cid)

        pr_total = sum((a.amount for a in c.all_adjustments() if a.group == "PR"), ZERO)
        if c.patient_resp is not None and self.off(c.patient_resp, pr_total):
            self.warn("PATIENT_RESP_MISMATCH",
                      f"CLP05 {c.patient_resp} != sum of PR adjustments {pr_total}", c.seg, cid)
        if c.status == "4" and c.paid != ZERO:
            self.warn("DENIED_WITH_PAYMENT", f"Claim status 4 (denied) but CLP04 = {c.paid}", c.seg, cid)
        if c.status == "22" and c.charge > ZERO:
            self.warn("REVERSAL_SIGN", "Claim status 22 (reversal) usually carries negative amounts",
                      c.seg, cid)


# --------------------------------------------------------------------------- CLI
EDI_SUFFIXES = {".835", ".edi", ".txt", ".x12", ".dat"}


def collect_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in EDI_SUFFIXES)
        else:
            files.append(p)
    return files


def read_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def validate_file(path: Path, tolerance: Decimal = ZERO) -> Validator835:
    return Validator835(str(path), read_text(path), tolerance).run()


def _jsonable(v):
    return str(v) if isinstance(v, Decimal) else v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate X12 835 remittance files.")
    ap.add_argument("paths", nargs="+", help="835 files or directories")
    ap.add_argument("--csv", help="write all findings to this CSV")
    ap.add_argument("--json", help="write findings and stats to this JSON file")
    ap.add_argument("--claims-csv", help="write a claim-level extract (for reconciliation)")
    ap.add_argument("--tolerance", default="0.00", help="allowed rounding difference in balancing")
    ap.add_argument("--strict", action="store_true", help="treat warnings as failures")
    ap.add_argument("--quiet", action="store_true", help="print summaries only")
    args = ap.parse_args(argv)

    files = collect_files(args.paths)
    if not files:
        print("No files found.", file=sys.stderr)
        return 2

    tol = Decimal(args.tolerance)
    results, all_findings, all_claims = [], [], []
    exit_code = 0
    for f in files:
        try:
            v = validate_file(f, tol)
        except OSError as exc:
            print(f"[UNREADABLE] {f}: {exc}", file=sys.stderr)
            exit_code = 2
            continue
        errors = sum(1 for x in v.findings if x.severity == "ERROR")
        warnings = len(v.findings) - errors
        status = "FAIL" if errors or (args.strict and warnings) else "PASS"
        if status == "FAIL":
            exit_code = max(exit_code, 1)
        s = v.stats
        print(f"\n[{status}] {f}")
        print(f"  segments={s['segments']} transactions={s['transactions']} claims={s['claims']} "
              f"total_paid={s['total_paid']} errors={errors} warnings={warnings}")
        if not args.quiet:
            for x in v.findings:
                loc = f"seg {x.segment_index}" if x.segment_index else "file"
                clm = f" claim={x.claim_id}" if x.claim_id else ""
                print(f"  {x.severity:<7} {x.rule:<22} {loc:<8}{clm}  {x.message}")
        all_findings += v.findings
        all_claims += v.claim_rows
        results.append({"file": str(f), "status": status, "errors": errors, "warnings": warnings,
                        **{k: _jsonable(val) for k, val in s.items()}})

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(Finding.__dataclass_fields__))
            w.writeheader()
            w.writerows(asdict(x) for x in all_findings)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"files": results, "findings": [asdict(x) for x in all_findings]}, fh, indent=2)
    if args.claims_csv and all_claims:
        with open(args.claims_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_claims[0]))
            w.writeheader()
            w.writerows({k: _jsonable(v) for k, v in r.items()} for r in all_claims)

    print(f"\n{len(files)} file(s): {sum(r['status'] == 'PASS' for r in results)} passed, "
          f"{sum(r['status'] == 'FAIL' for r in results)} failed")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
