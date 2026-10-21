"""
Extract candidate records (name, party, funding, website, office, district,
biography, statement) from the Arizona Citizens Clean Elections Commission
2026 General Election Voter Education Guide PDF.

The guide's candidate pamphlet pages are laid out very regularly:

    CANDIDATES FOR <OFFICE>          <- one office header per page
    <Candidate Name>
    Party: X   Funding: Y   Website: Z
    Biography: ...
    Statement: ...
    <boilerplate footer>
    <page number>

so the parse keys off the "Party: ... Funding: ..." line: the line above it is
the candidate's name, and everything below it until the next such line (or the
end of the page) is that candidate's biography + statement.

Requires the `pdftotext` binary (poppler): brew install poppler
Output: data/candidate_statements.json and data/candidate_statements.csv
"""

import csv
import json
import os
import re
import subprocess
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

PDF = "../1509-1497AllDistrictWebEnglishv2.pdf"
OUT_JSON = "../data/candidate_statements.json"
OUT_CSV = "../data/candidate_statements.csv"

# Page furniture to drop before parsing.
FURNITURE = (
    re.compile(r"^Candidate Statement Pamphlet General Election$"),
    re.compile(r"^The statements were reproduced exactly as submitted"),
    re.compile(r"^These statements represent the opinions of the authors"),
    re.compile(r"^and were not edited for spelling, grammar, or punctuation\.?$"),
    re.compile(r"^\d{1,3}$"),                      # page number
    re.compile(r"^2\s?0\s?2\s?6\s+V.*G\s?U\s?I\s?D\s?E$"),  # letter-spaced running head
)

HEADER_RE = re.compile(r"CANDIDATES FOR\s+(.+?)\s*$")
# "Party: Republican   Funding: Traditional   Website: example.com"
PARTY_RE = re.compile(
    r"^Party:\s*(?P<party>.*?)\s+Funding:\s*(?P<funding>\S+)"
    r"(?:\s+Website:\s*(?P<website>.*?))?\s*$"
)
# "STATE REPRESENTATIVE - DISTRICT 26", "PIMA BOARD OF SUPERVISORS DIST. 5"
DISTRICT_RE = re.compile(r"^(?P<office>.*?)\s*[-–]?\s*DIST(?:RICT|\.)\s*(?P<district>\d+)\s*$")

NONE_PROVIDED = re.compile(r"^none provided\.?$", re.I)


def pdf_pages(path):
    """Return the PDF's pages as lists of stripped, de-furnitured lines."""
    text = subprocess.run(
        ["pdftotext", "-layout", path, "-"],
        check=True, capture_output=True, text=True,
    ).stdout
    pages = []
    for raw in text.split("\f"):
        lines = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or any(p.match(line) for p in FURNITURE):
                continue
            lines.append(line)
        pages.append(lines)
    return pages


def split_office(header):
    """'STATE SENATOR - DISTRICT 10' -> ('STATE SENATOR', '10')."""
    m = DISTRICT_RE.match(header)
    if m:
        return m.group("office").strip(), m.group("district")
    return header, None


def parse_block(lines):
    """Join a candidate's body lines and split into biography / statement."""
    body = " ".join(lines)
    body = re.sub(r"\s+", " ", body).strip()

    bio, stmt = "", ""
    m_bio = re.search(r"\bBiography:\s*", body)
    m_stmt = re.search(r"\bStatement:\s*", body)
    if m_bio:
        end = m_stmt.start() if m_stmt and m_stmt.start() > m_bio.end() else len(body)
        bio = body[m_bio.end():end].strip()
    if m_stmt:
        stmt = body[m_stmt.end():].strip()

    if NONE_PROVIDED.match(bio):
        bio = ""
    if NONE_PROVIDED.match(stmt):
        stmt = ""
    return bio, stmt


def extract(path):
    records = []
    warnings = []

    for page_idx, lines in enumerate(pdf_pages(path)):
        header_i = next((i for i, l in enumerate(lines) if HEADER_RE.search(l)), None)
        if header_i is None:
            continue
        office_full = HEADER_RE.search(lines[header_i]).group(1).strip()
        office, district = split_office(office_full)

        party_idx = [i for i, l in enumerate(lines) if PARTY_RE.match(l)]
        if not party_idx:
            warnings.append(f"page {page_idx + 1}: office header but no candidates")
            continue

        # Text between the office header and the first candidate's name would be
        # a statement spilling over from the previous page; flag it if seen.
        if party_idx[0] - 1 > header_i + 1:
            warnings.append(
                f"page {page_idx + 1}: unexpected text before first candidate: "
                + " | ".join(lines[header_i + 1:party_idx[0] - 1])[:200]
            )

        for n, pi in enumerate(party_idx):
            name = lines[pi - 1] if pi - 1 > header_i else ""
            end = party_idx[n + 1] - 1 if n + 1 < len(party_idx) else len(lines)
            m = PARTY_RE.match(lines[pi])
            bio, stmt = parse_block(lines[pi + 1:end])

            records.append({
                "candidate_id": f"p{page_idx + 1:03d}-{n + 1}",
                "name": name,
                "party": m.group("party").strip(),
                "funding": m.group("funding").strip(),
                "website": (m.group("website") or "").strip(),
                "office": office,
                "district": district,
                "office_full": office_full,
                "pdf_page": page_idx + 1,
                "biography": bio,
                "statement": stmt,
                "statement_word_count": len(stmt.split()),
            })

    return records, warnings


def main():
    if not os.path.exists(PDF):
        sys.exit(f"PDF not found: {PDF}")

    records, warnings = extract(PDF)

    with open(OUT_JSON, "w") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    cols = ["candidate_id", "name", "party", "funding", "website", "office",
            "district", "office_full", "pdf_page", "statement_word_count",
            "biography", "statement"]
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(records)

    no_stmt = [r["name"] for r in records if not r["statement"]]
    print(f"candidates: {len(records)}")
    print(f"with statement: {len(records) - len(no_stmt)}  without: {len(no_stmt)}")
    print(f"offices: {len({r['office_full'] for r in records})}")
    for w_ in warnings:
        print("WARN:", w_)
    if no_stmt:
        print("no statement:", ", ".join(no_stmt))


if __name__ == "__main__":
    main()
