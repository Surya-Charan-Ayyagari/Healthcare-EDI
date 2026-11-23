"""Generate a clean sample 835 and a deliberately broken one for testing the validator."""
from pathlib import Path

ISA = ("ISA*00*          *00*          *ZZ*PAYERID        *ZZ*PROVIDERID     "
       "*261115*1200*^*00501*000000101*0*P*:")
assert len(ISA) == 105


def body(bpr_amount="285.00", svc2_paid="60", clp2_status="1", cas_group="CO", dtm="20261101"):
    return [
        f"BPR*I*{bpr_amount}*C*ACH*CCP*01*999999999*DA*123456*1512345678**01*888888888*DA*98765*20261115",
        "TRN*1*12345678*1512345678",
        "DTM*405*20261114",
        "N1*PR*ACME HEALTH PLAN",
        "N3*100 MAIN ST",
        "N4*NEW YORK*NY*10001",
        "PER*BL*EDI SUPPORT*TE*8005551212",
        "N1*PE*SAMPLE CLINIC*XX*1234567893",
        "N3*200 OAK AVE",
        "N4*BROOKLYN*NY*11201",
        "LX*1",
        # Claim 1: paid 150 on 200 charge, two lines
        f"CLP*CLM001*{clp2_status}*200*150*30*12*PCN0001*11*1",
        "NM1*QC*1*DOE*JOHN****MI*MEM001",
        f"DTM*232*{dtm}",
        "SVC*HC:99213*120*90**1",
        "DTM*472*20261101",
        "CAS*CO*45*10",
        "CAS*PR*2*20",
        f"SVC*HC:85025*80*{svc2_paid}**1",
        "DTM*472*20261101",
        "CAS*PR*3*10",
        f"CAS*{cas_group}*45*10",
        # Claim 2: denied
        "CLP*CLM002*4*150*0*0*12*PCN0002*11*1",
        "NM1*QC*1*ROE*JANE****MI*MEM002",
        "SVC*HC:97110*150*0**1",
        "DTM*472*20261102",
        "CAS*CO*50*150",
        # Claim 3: single line
        "CLP*CLM003*1*175*145*0*12*PCN0003*11*1",
        "NM1*QC*1*POE*SAM****MI*MEM003",
        "SVC*HC:99214*175*145**1",
        "DTM*472*20261103",
        "CAS*CO*45*30",
        # Provider-level recoupment of 10 → BPR02 = 295 - 10 = 285
        "PLB*1234567893*20261231*WO:CLM999*10",
    ]


def build(segs, se_count=None, iea_ctrl="000000101"):
    st = ["ST*835*0001"] + segs
    n = se_count if se_count is not None else len(st) + 1
    all_segs = [ISA, "GS*HP*PAYERID*PROVIDERID*20261115*1200*101*X*005010X221A1",
                *st, f"SE*{n}*0001", "GE*1*101", f"IEA*1*{iea_ctrl}"]
    return "~\n".join(all_segs) + "~\n"


out = Path(__file__).parent / "samples"
out.mkdir(exist_ok=True)
(out / "valid_835.edi").write_text(build(body()))
(out / "invalid_835.edi").write_text(build(
    body(bpr_amount="300.00",     # transaction out of balance
         svc2_paid="65",          # service line + claim out of balance
         clp2_status="9",         # invalid claim status
         cas_group="XX",          # invalid CAS group code
         dtm="20261341"),         # impossible date
    se_count=99,                  # wrong SE01 count
    iea_ctrl="000000999"))        # IEA02 != ISA13
print("Wrote", *sorted(p.name for p in out.iterdir()))
