"""Synthetic patients + PA requests. No real data. Fixed seed so reruns match."""
import argparse, json, random, hashlib
from pathlib import Path

FIRST = ["Alex", "Sam", "Priya", "Ravi", "Maria", "John", "Anita", "Wei", "Noah", "Leah", "Arjun", "Emma"]
LAST = ["Smith", "Patel", "Garcia", "Sharma", "Lee", "Brown", "Iyer", "Cohen", "Nguyen", "Khan"]
DOC_TYPES = ["clinic_note", "pt_record", "xray_report", "neuro_exam", "lab_cbc",
             "imaging_report", "admission_note", "discharge_summary", "estimate", "id_proof"]


def facts(t, r):
    # what a reviewer would pull out of each doc. synthetic, rough spread so some cases fail
    return {
        "clinic_note": lambda: {"indication": r.random() < 0.85},
        "pt_record": lambda: {"weeks": r.randint(1, 12), "improved": r.random() < 0.3},
        "neuro_exam": lambda: {"deficit": r.random() < 0.7},
        "imaging_report": lambda: {"abnormal": r.random() < 0.8},
        "xray_report": lambda: {"abnormal": r.random() < 0.5},
        "estimate": lambda: {"amount": r.randrange(5000, 60000, 500)},
        "id_proof": lambda: {"verified": r.random() < 0.95},
        "admission_note": lambda: {"admitted": r.random() < 0.9},
        "lab_cbc": lambda: {"hb": round(r.uniform(6, 16), 1)},
        "primary_eob": lambda: {"outcome": r.choice(["paid", "denied"]),
                                "paid_amount": r.randrange(0, 40000, 500)},
    }.get(t, dict)()


US_SERVICES = ["MRI-LSPINE", "CT-HEAD", "PT-12", "KNEE-ARTHRO", "SLEEP-STUDY"]
IN_SERVICES = ["MRI-LSPINE", "CATARACT", "ANGIO", "APPENDECTOMY", "DIALYSIS"]


def make(seed=42, n_patients=300, n_us=120, n_in=80):
    rnd = random.Random(seed)
    frnd = random.Random(seed + 1)  # separate stream so doc facts don't shift who gets which docs
    patients = []
    for i in range(n_patients):
        country = "US" if i < n_patients * 0.6 else "IN"
        primary = "tenant_us_acme" if country == "US" else "tenant_in_bharat"
        cov = [{"order": 1, "plan": f"{primary}:basic", "tenant": primary}]
        # ~10% get a second payer for the COB bit (US only, e.g. Medicare + supplemental)
        dual = country == "US" and rnd.random() < 0.1
        if dual:
            cov.append({"order": 2, "plan": "medigap_demo:supp", "tenant": "medigap_demo"})
        docs = [{"type": t, "date": f"2026-{rnd.randint(1, 9):02d}-{rnd.randint(1, 28):02d}"}
                for t in rnd.sample(DOC_TYPES, rnd.randint(1, 6))]
        # half of dual-coverage patients already have the primary EOB on file
        if dual and rnd.random() < 0.5:
            docs.append({"type": "primary_eob",
                         "date": f"2026-{rnd.randint(1, 9):02d}-{rnd.randint(1, 28):02d}"})
        for d in docs:
            d["facts"] = facts(d["type"], frnd)
        patients.append({
            "id": f"pt-{i:04d}",
            "name": f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
            "birth_year": rnd.randint(1940, 2005),
            "country": country,
            "coverage": cov,
            "documents": docs,
        })

    us_pts = [p for p in patients if p["country"] == "US"]
    in_pts = [p for p in patients if p["country"] == "IN"]
    requests = []
    for tenant, pts, services, n in [("tenant_us_acme", us_pts, US_SERVICES, n_us),
                                     ("tenant_in_bharat", in_pts, IN_SERVICES, n_in)]:
        for k in range(n):
            p = rnd.choice(pts)
            svc = rnd.choice(services)
            urgent = rnd.random() < 0.2
            kind = "pa" if tenant == "tenant_us_acme" else rnd.choice(["cashless", "discharge"])
            body = {
                "resourceType": "Claim",
                "use": "preauthorization",
                "tenant": tenant,
                "kind": kind,
                "patient": {"reference": f"Patient/{p['id']}"},
                "priority": {"coding": [{"code": "stat" if urgent else "normal"}]},
                "item": [{"productOrService": {"coding": [{"code": svc}]}}],
                "supportingInfo": [{"category": d["type"], "value": d["facts"]} for d in p["documents"]],
                "seq": k,
            }
            body["id"] = "case-" + hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10]
            requests.append(body)
    return patients, requests


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    pts, reqs = make(a.seed)
    out = Path(a.out); out.mkdir(exist_ok=True)
    (out / "patients.json").write_text(json.dumps(pts, indent=1))
    (out / "requests.json").write_text(json.dumps(reqs, indent=1))
    print(f"{len(pts)} patients, {len(reqs)} requests -> {out}/")
