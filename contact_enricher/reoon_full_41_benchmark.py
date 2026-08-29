from __future__ import annotations

import asyncio
import csv
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .enricher import crawl_company
from .reoon_verifier import check_reoon_balance, verify_email_reoon

BASE = Path(__file__).resolve().parent
INPUT_PATH = BASE / "benchmark_batch_106.csv"
OUTPUT_DIR = Path("/app/output")
OUTPUT_CSV = OUTPUT_DIR / "reoon-full-41-with-generic-fallback.csv"
SUMMARY_JSON = OUTPUT_DIR / "reoon-full-41-summary.json"

# Recovered person-first benchmark candidates. These are the exact 41 primary
# person-specific candidates produced by the completed 106-company benchmark.
CANDIDATES = {
    1: ("Chris Chabrowski", "Sales Director at Robots International", "https://www.linkedin.com/in/chris-chabrowski-37050241a", "chris@robotsinternational.com"),
    2: ("Matthew Polito", "Digital marketing strategy", "https://www.linkedin.com/in/matthewpolito/", "matthew@global.toshiba"),
    3: ("Adam Petrick", "CMO / Chief Brand Officer", "https://www.linkedin.com/in/adampetrick/", "adam@sharkninja.com"),
    5: ("Ryan Brodley", "Business Development", "https://www.linkedin.com/in/ryanbrodley", "ryan@jobyaviation.com"),
    7: ("Andrew Coppin", "Founder & CEO", "https://www.linkedin.com/in/coppo888", "andrew@farm.bot"),
    8: ("Ross Kingdon", "Senior Principal Operations Program", "https://www.linkedin.com/in/ross-a-kingdon", "ross@berkshiregrey.com"),
    9: ("Michelle Thoras", "Director - Business Development", "https://in.linkedin.com/in/michelle-thoras-5b988519", "michelle@grandviewresearch.com"),
    12: ("John Gehre", "CEO", "https://www.linkedin.com/in/john-gehre", "john@badger-technologies.com"),
    14: ("Shakir Dzheyranov", "CEO / Brand design", "https://www.linkedin.com/in/shakir-works", "shakir@openrobotics.org"),
    22: ("Lee-Martin Seymour", "Founder & CEO", "https://au.linkedin.com/in/leemartinseymour", "leemartin@builtin.com"),
    25: ("Paige Labrador", "Senior Marketing Manager", "https://www.linkedin.com/in/paige-labrador-36b615148", "paige@ozobot.com"),
    26: ("Megan Hughes", "Travel Writer / Editor / Branded", "https://ca.linkedin.com/in/megan-hughes-36b269100", "megan@nationalgeographic.com"),
    30: ("Tej Yale", "Business Development and Marketing", "https://www.linkedin.com/in/tejyale", "tej@ds-automotion.com"),
    32: ("Marie Vanderpuye", "Program Management / Sustainability", "https://gh.linkedin.com/in/marie-vanderpuye-b8148547", "marie@impactlab.org"),
    34: ("Gary Giger Ph.D.", "Principal Engineer", "https://www.linkedin.com/in/garyfredgiger", "gary@iamrobotics.com"),
    35: ("Stefan Stockl", "Digital Marketing Manager", "https://de.linkedin.com/in/stefanstoeckl", "stefan@heidenhain.de"),
    36: ("Erick Hachenburg", "CEO / Founder", "https://www.linkedin.com/in/erickhachenburg", "erick@creator.rest"),
    38: ("Doug Lane", "Business Development Manager", "https://www.linkedin.com/in/mrdouglane", "doug@bearrobotics.ai"),
    39: ("Alix Oudin", "Head of Marketing & Communication", "https://fr.linkedin.com/in/alix-oudin", "alix@runningbrainsrobotics.com"),
    40: ("Mike Cornelison", "Owner / Automation", "https://www.linkedin.com/in/mike-cornelison-561b7b78", "mike@nachi-robotics.com"),
    45: ("Jason Stephen", "Product Marketing Manager", "https://www.linkedin.com/in/jasonstephen", "jason@midea.com"),
    46: ("Brent Smart", "Chief Marketing Officer", "https://au.linkedin.com/in/brent-smart-71308037", "brent@smartconservationtools.org"),
    51: ("Christy Marble", "Chief Marketing Officer", "https://www.linkedin.com/in/christymarble", "christy@marble.io"),
    52: ("Kenny Lee", "Co-Founder & CEO", "https://www.linkedin.com/in/kennyklee", "kenny@aigen.com"),
    53: ("John Wood", "President and Chairman", "https://www.linkedin.com/in/john-wood-sdr", "john@sallydarkrides.com"),
    56: ("Brendan Loudermilk", "Projects / Growth", "https://www.linkedin.com/in/loudermilk", "brendan@climatescape.org"),
    57: ("Mudit Kulshrestha", "Growth & Partnerships", "https://in.linkedin.com/in/mkulshre", "mudit@intuitive.com"),
    61: ("Brett Goodwin", "Vice President Marketing", "https://www.linkedin.com/in/goodwinbrett", "brett@carbonrobotics.com"),
    65: ("David Pinn", "CEO", "https://www.linkedin.com/in/dpinn", "david@braincorp.com"),
    66: ("Yang Liu", "Regional Director", "https://cn.linkedin.com/in/yang-liu-80329410b", "yang@hairobotics.com"),
    67: ("Gajan Mohanarajah", "Chief Executive Officer", "https://jp.linkedin.com/in/gajan-mohanarajah-973b00100", "gajan@rapyuta-robotics.com"),
    68: ("Jordan Barker", "Principal", "https://ca.linkedin.com/in/jordan-barker-6548483", "jordan@code.europa.eu"),
    69: ("Darwon Choe", "Head of Strategic Marketing", "https://www.linkedin.com/in/darwonchoe", "darwon@woven.toyota"),
    76: ("Chrissa Ranis", "Founder & CEO", "https://ph.linkedin.com/in/chrissa-ranis-574804112", "chrissa@chrissaibiernas.com"),
    83: ("Anne Charls", "Senior Vice President, Marketing", "https://www.linkedin.com/in/annecharls", "anne.charls@autostoresystem.com"),
    85: ("Annie Handrick", "Senior Partner Marketing Manager", "https://www.linkedin.com/in/anniehandrick", "annie@starship.xyz"),
    89: ("Bob Raida", "CEO", "https://www.linkedin.com/in/bobraida", "bob@hebirobotics.com"),
    90: ("Eleonore Crespo", "Co-Founder and Co-CEO", "https://www.linkedin.com/in/eleonorecrespo", "eleonore@supplychaindigital.com"),
    95: ("Klaus Hirschle", "Regional President / Managing Director", "https://de.linkedin.com/in/klaus-hirschle", "klaus@kaercher.com"),
    97: ("Hiroaki Itaya", "General Manager", "https://www.linkedin.com/in/hiroaki-itaya-061a1511", "hiroaki@intelligentactuator.com"),
    98: ("Elisa Cortopassi", "Director of Sales", "https://www.linkedin.com/in/elisa-cortopassi-683b902b", "elisa.cortopassi@grenzebach.com"),
}

GENERIC_PREFIXES = [
    "support", "contact", "info", "sales", "hello", "marketing", "press", "media",
    "pr", "communications", "partnerships", "events", "business", "global", "service"
]
GENERIC_EXCLUDE = [
    "noreply", "no-reply", "privacy", "legal", "careers", "jobs", "complaint", "abuse",
    "billing", "security", "webmaster"
]


def load_rows() -> list[dict]:
    with INPUT_PATH.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return rows[:106]


def production_status(verdict: str) -> str:
    verdict = (verdict or "unknown").lower()
    if verdict == "valid":
        return "Ready"
    if verdict in {"catch_all", "unknown", "risky"}:
        return "Review"
    if verdict == "invalid":
        return "Reject"
    return "Not checked"


def choose_generic(emails) -> tuple[str, str]:
    clean = []
    for item in emails:
        email = str(getattr(item, "email", "") or "").strip().lower()
        if not email or "@" not in email:
            continue
        local = email.split("@", 1)[0]
        if any(term in local for term in GENERIC_EXCLUDE):
            continue
        clean.append((email, str(getattr(item, "source_url", "") or "")))
    if not clean:
        return "", ""

    def score(pair):
        local = pair[0].split("@", 1)[0]
        for idx, prefix in enumerate(GENERIC_PREFIXES):
            if local == prefix or local.startswith(prefix + ".") or local.startswith(prefix + "-"):
                return 100 - idx
        return 1

    clean.sort(key=score, reverse=True)
    best = clean[0]
    return best[0], best[1]


async def verify_all_candidates() -> dict[int, dict]:
    semaphore = asyncio.Semaphore(3)
    results: dict[int, dict] = {}

    async def one(row_num: int, email: str):
        async with semaphore:
            result = await verify_email_reoon(email, mode="power")
            results[row_num] = result
            print("REOON_41_ROW " + json.dumps({"benchmark_row": row_num, **result}, ensure_ascii=False), flush=True)

    await asyncio.gather(*[
        one(row_num, values[3]) for row_num, values in CANDIDATES.items()
    ])
    return results


async def collect_generic_fallbacks(rows: list[dict], verification: dict[int, dict]) -> dict[int, dict]:
    semaphore = asyncio.Semaphore(4)
    output: dict[int, dict] = {}

    async def one(row_num: int, row: dict):
        candidate = CANDIDATES.get(row_num)
        verdict = str(verification.get(row_num, {}).get("verdict") or "not_run").lower()
        # Ready named emails do not need a generic fallback. Every other row can
        # benefit from a public role inbox for outreach continuity.
        if candidate and verdict == "valid":
            output[row_num] = {"email": "", "source": "", "status": "not_needed"}
            return
        async with semaphore:
            try:
                emails, _contacts, _visited = await asyncio.wait_for(
                    crawl_company(str(row.get("Website URL") or ""), max_pages=4),
                    timeout=35,
                )
                email, source = choose_generic(emails)
                output[row_num] = {"email": email, "source": source, "status": "found" if email else "none"}
            except Exception as exc:
                output[row_num] = {"email": "", "source": "", "status": "error", "error": str(exc)[:180]}

    await asyncio.gather(*[one(i, row) for i, row in enumerate(rows, start=1)])
    return output


def write_outputs(rows: list[dict], verification: dict[int, dict], generics: dict[int, dict], balance_before: dict, balance_after: dict) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_rows = []
    counts = {"Ready": 0, "Review": 0, "Reject": 0, "Not checked": 0}
    generic_found = 0

    for row_num, row in enumerate(rows, start=1):
        candidate = CANDIDATES.get(row_num)
        verify = verification.get(row_num, {})
        verdict = str(verify.get("verdict") or "not_run").lower()
        status = production_status(verdict) if candidate else "Not checked"
        if candidate:
            counts[status] = counts.get(status, 0) + 1
        generic = generics.get(row_num, {})
        generic_email = str(generic.get("email") or "")
        if generic_email:
            generic_found += 1

        target_email = candidate[3] if candidate else ""
        preferred = ""
        preferred_type = ""
        if candidate and status == "Ready":
            preferred = target_email
            preferred_type = "Verified person"
        elif generic_email:
            preferred = generic_email
            preferred_type = "Public generic fallback"
        elif candidate and status == "Review":
            preferred = target_email
            preferred_type = "Person candidate - review"

        out_rows.append({
            "Benchmark Row": row_num,
            "Company": row.get("Company", ""),
            "Website URL": row.get("Website URL", ""),
            "Contact Name": candidate[0] if candidate else "",
            "Job Title": candidate[1] if candidate else "",
            "LinkedIn URL": candidate[2] if candidate else "",
            "Person Candidate Email": target_email,
            "Reoon Verdict": verdict if candidate else "not_run",
            "Reoon Raw Status": verify.get("raw_status", "") if candidate else "",
            "Reoon Score": verify.get("overall_score", "") if candidate else "",
            "Reoon Safe To Send": verify.get("is_safe_to_send", "") if candidate else "",
            "Production Person Status": status,
            "Generic Fallback Email": generic_email,
            "Generic Fallback Source": generic.get("source", ""),
            "Preferred Contact Email": preferred,
            "Preferred Contact Type": preferred_type,
            "Verification Error": verify.get("error", "") if candidate else "",
            "Generic Lookup Status": generic.get("status", ""),
        })

    fields = list(out_rows[0].keys())
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(out_rows)

    verification_errors = sum(1 for row_num in CANDIDATES if verification.get(row_num, {}).get("error"))
    summary = {
        "companies": len(rows),
        "person_candidates": len(CANDIDATES),
        "person_ready": counts.get("Ready", 0),
        "person_review": counts.get("Review", 0),
        "person_reject": counts.get("Reject", 0),
        "person_not_checked": counts.get("Not checked", 0),
        "verification_errors": verification_errors,
        "generic_fallbacks_found": generic_found,
        "contactable_with_preferred_email": sum(1 for item in out_rows if item.get("Preferred Contact Email")),
        "balance_before": balance_before,
        "balance_after": balance_after,
        "output": str(OUTPUT_CSV),
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("REOON_41_SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"REOON_41_CSV {OUTPUT_CSV}", flush=True)
    return summary


async def main() -> None:
    rows = load_rows()
    balance_before = await check_reoon_balance()
    print("REOON_41_BALANCE_BEFORE " + json.dumps(balance_before), flush=True)
    verification = await verify_all_candidates()
    generics = await collect_generic_fallbacks(rows, verification)
    balance_after = await check_reoon_balance()
    print("REOON_41_BALANCE_AFTER " + json.dumps(balance_after), flush=True)
    write_outputs(rows, verification, generics, balance_before, balance_after)


def serve_outputs() -> None:
    os.chdir(OUTPUT_DIR)
    port = int(os.getenv("PORT", "8080"))
    print(f"REOON_41_FILE_SERVER http://0.0.0.0:{port}/{OUTPUT_CSV.name}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler).serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
    serve_outputs()
