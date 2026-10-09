#!/usr/bin/env python3
"""Publish an accepted memo from its private submission repository.

Rules:
- Only the files in the memo's publish/ folder are published (on Zenodo and in
  the public memos repository). Drafts and everything else stay private.
- Versions are whole numbers: 1 for the first publication, then 2, 3, ...
  Each published version is identified as <memo ID>.v<number>, e.g. SV-2026-001.v2.
- The memo ID is assigned at the first publication and never changes.
- The publication history is read from the public repository, which only this
  automation writes to, so editing memo.yaml by hand cannot change the memo ID
  or publish the same version twice.

The log of this script is public (memo-intake is a public repository), so it
never prints the memo's title, authors or abstract.
"""

import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

MEMO_DIR = Path(os.environ["MEMO_DIR"])
PUBLIC_DIR = Path(os.environ["PUBLIC_DIR"])
PUBLIC_REPO_URL = os.environ["PUBLIC_REPO_URL"].rstrip("/")
SUBMISSION = str(os.environ.get("SUBMISSION_NUMBER", "")).strip()
MEMO_REPO_URL = os.environ.get("MEMO_REPO_URL", "").rstrip("/")
ZENODO_API = os.environ.get("ZENODO_API", "https://sandbox.zenodo.org/api").rstrip("/")
ZENODO_SITE = ZENODO_API[: -len("/api")] if ZENODO_API.endswith("/api") else ZENODO_API
ZENODO_TOKEN = os.environ["ZENODO_TOKEN"]
ZENODO_COMMUNITY = os.environ.get("ZENODO_COMMUNITY", "").strip()

# Prefix used in the memo ID for each series (e.g. SV-2026-001)
SERIES_PREFIXES = {
    "Science Verification": "SV",
    "Commissioning": "COM",
    "Engineering": "ENG",
}
PLACEHOLDERS = {"", "memo title", "author name", "affiliation", "short summary of the memo."}
ID_PATTERN = re.compile(r"[A-Z]+-\d{4}-\d{3}")
MEMOS = PUBLIC_DIR / "memos"


# ---------------------------------------------------------------- helpers

def info(message):
    print(message, flush=True)


def fail(message):
    print(f"::error::{message}", flush=True)
    sys.exit(1)


def set_output(name, value):
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def is_placeholder(value):
    return value is None or str(value).strip().lower() in PLACEHOLDERS


def paragraphs(text):
    """Split the abstract into paragraphs (works for plain, '>' and '|' YAML styles)."""
    text = str(text or "").strip()
    blank_line = re.compile(r"\n\s*\n")
    chunks = blank_line.split(text) if blank_line.search(text) else text.split("\n")
    return [" ".join(c.split()) for c in chunks if c.strip()]


def set_field(text, key, value):
    """Set a top-level field in memo.yaml, keeping any comment on its line."""
    line = f"{key}: {value}"
    pattern = re.compile(rf"^{re.escape(key)}:[^\n#]*(#.*)?$", re.M)
    match = pattern.search(text)
    if match:
        comment = f"  {match.group(1)}" if match.group(1) else ""
        return text[: match.start()] + line + comment + text[match.end():]
    return text.rstrip("\n") + f"\n{line}\n"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True)


def commit_and_push(repo, message):
    git(repo, "add", "-A")
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode == 0:
        info("  No changes to commit.")
        return
    git(repo, "commit", "-q", "-m", message)
    git(repo, "push", "-q", "origin", "HEAD")


def version_number(value):
    """Return a version as a whole number (1, 2, 3...), or None if it is not one."""
    text = str(value).strip() if value is not None else ""
    text = text[1:] if text.lower().startswith("v") else text
    return int(text) if re.fullmatch(r"[1-9]\d*", text) else None


# ---------------------------------------------------------- checks on the memo

def load_metadata():
    path = MEMO_DIR / "memo.yaml"
    if not path.is_file():
        fail("memo.yaml was not found in the submission repository.")
    text = path.read_text(encoding="utf-8")
    try:
        meta = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        fail(f"memo.yaml is not valid YAML{where}.")
    if not isinstance(meta, dict):
        fail("memo.yaml must contain a set of fields (key: value).")

    problems = []
    if is_placeholder(meta.get("title")):
        problems.append("title")
    authors = meta.get("authors")
    if not isinstance(authors, list) or not authors:
        problems.append("authors")
    elif any(not isinstance(a, dict) or is_placeholder(a.get("name")) for a in authors):
        problems.append("authors (name)")
    if is_placeholder(meta.get("abstract")):
        problems.append("abstract")
    if problems:
        fail("memo.yaml is incomplete. Please fill in: " + ", ".join(problems) + ".")
    if version_number(meta.get("version")) is None:
        fail("The version in memo.yaml must be a whole number: 1 for the first publication, "
             "then 2, 3, ... for each new version.")
    return text, meta


def files_to_publish():
    folder = MEMO_DIR / "publish"
    if not folder.is_dir():
        fail("The publish/ folder was not found. Put the final memo PDF in publish/.")
    entries = sorted(p for p in folder.iterdir() if not p.name.startswith("."))
    if any(p.is_dir() for p in entries):
        fail("The publish/ folder must not contain subfolders.")
    if not any(p.suffix.lower() == ".pdf" for p in entries):
        fail("The publish/ folder must contain the memo PDF.")
    info("Files in publish/ (only these will be published):")
    for p in entries:
        info(f"  - {p.name} ({p.stat().st_size / 1024:.0f} kB)")
    return entries


def published_names(files, label):
    """If there is a single PDF, publish it as <memo ID>.v<number>.pdf; other files keep their names."""
    pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
    return [(p, f"{label}.pdf" if len(pdfs) == 1 and p in pdfs else p.name) for p in files]


# --------------------------------------------- publication history (public repo)

def published_history():
    """Find the earlier publications of this submission in the public repository."""
    found = {}
    for vfile in MEMOS.glob("*/v*/memo.yaml"):
        number = version_number(vfile.parent.name)
        if number is None:
            continue
        try:
            vmeta = yaml.safe_load(vfile.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if SUBMISSION and str(vmeta.get("submission_request")) == SUBMISSION:
            found.setdefault(vfile.parent.parent.name, []).append((number, vmeta))
    if len(found) > 1:
        fail(f"Submission #{SUBMISSION} appears under more than one memo ID in the public repository "
             f"({', '.join(sorted(found))}). An editor must resolve this by hand.")
    if not found:
        return None, []
    memo_id, versions = next(iter(found.items()))
    return memo_id, sorted(versions, key=lambda v: v[0])


def new_memo_id(meta):
    prefix = SERIES_PREFIXES.get(str(meta.get("series") or "").strip(), "MEMO")
    year = dt.datetime.now(dt.timezone.utc).year
    pattern = re.compile(rf"{prefix}-{year}-(\d{{3}})")
    numbers = [int(m.group(1)) for d in MEMOS.glob("*") if (m := pattern.fullmatch(d.name))]
    return f"{prefix}-{year}-{max(numbers, default=0) + 1:03d}"


# ------------------------------------------------------------------ Zenodo

session = requests.Session()
session.headers["Authorization"] = f"Bearer {ZENODO_TOKEN}"


def zenodo(method, url, what, **kwargs):
    try:
        response = session.request(method, url, timeout=300, **kwargs)
    except requests.RequestException as error:
        fail(f"Zenodo: {what} failed: {error}")
    if not response.ok:
        fail(f"Zenodo: {what} failed (HTTP {response.status_code}): {response.text[:500]}")
    return response.json() if response.content else {}


def lookup(url):
    """GET a Zenodo URL without failing the run if it does not answer."""
    try:
        response = session.get(url, timeout=60)
        return response.json() if response.ok else {}
    except (requests.RequestException, ValueError):
        return {}


def extract_doi(data, concept=False):
    """Find a DOI in a Zenodo response, whatever its exact shape."""
    data = data or {}
    meta = data.get("metadata") or {}
    if concept:
        candidates = [data.get("conceptdoi"), meta.get("conceptdoi")]
    else:
        candidates = [
            data.get("doi"),
            meta.get("doi"),
            (meta.get("prereserve_doi") or {}).get("doi"),
            ((data.get("pids") or {}).get("doi") or {}).get("identifier"),
        ]
        doi_url = data.get("doi_url") or ""
        if "doi.org/" in doi_url:
            candidates.append(doi_url.split("doi.org/", 1)[1])
    return next((str(c).strip() for c in candidates if c), "")


def zenodo_name(name):
    """Zenodo expects 'Family name, Given names'."""
    name = " ".join(str(name).split())
    if "," in name:
        return name
    given, _, family = name.rpartition(" ")
    return f"{family}, {given}" if given else name


def valid_orcid(value):
    digits = str(value or "").strip().replace("-", "").upper()
    if not re.fullmatch(r"\d{15}[\dX]", digits):
        return False
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return digits[-1] == ("X" if check == 10 else str(check))


def zenodo_metadata(meta, memo_id, number, today):
    creators = []
    for author in meta["authors"]:
        creator = {"name": zenodo_name(author["name"])}
        if not is_placeholder(author.get("affiliation")):
            creator["affiliation"] = str(author["affiliation"]).strip()
        if author.get("orcid"):
            if valid_orcid(author["orcid"]):
                creator["orcid"] = str(author["orcid"]).strip()
            else:
                info("  Note: an author ORCID is not valid and was left out of the Zenodo record.")
        creators.append(creator)

    data = {
        "upload_type": "publication",
        "publication_type": str(meta.get("resource_type") or "technicalnote"),
        "title": f"SKAO Memo {memo_id}: {str(meta['title']).strip()}",
        "creators": creators,
        "description": "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs(meta["abstract"])),
        "access_right": "open",
        "license": str(meta.get("license") or "cc-by-4.0").lower(),
        "version": f"v{number}",
        "publication_date": today,
        "notes": f"SKAO Memo Series (demo). Memo {memo_id}.v{number}. Fictitious test content.",
    }
    keywords = [str(k).strip() for k in (meta.get("keywords") or []) if str(k).strip()]
    if keywords:
        data["keywords"] = keywords
    if ZENODO_COMMUNITY:
        data["communities"] = [{"identifier": ZENODO_COMMUNITY}]
    return data


def publish_on_zenodo(meta, memo_id, number, uploads, today, previous_record):
    if previous_record:
        info(f"Creating {memo_id}.v{number} as a new version of Zenodo record {previous_record}...")
        result = zenodo("POST", f"{ZENODO_API}/deposit/depositions/{previous_record}/actions/newversion",
                        "creating a new version")
        draft_url = (result.get("links") or {}).get("latest_draft")
        if not draft_url:
            fail("Zenodo did not return a draft for the new version.")
        deposition = zenodo("GET", draft_url, "reading the new version draft")
        for old in deposition.get("files") or []:
            zenodo("DELETE", f"{ZENODO_API}/deposit/depositions/{deposition['id']}/files/{old['id']}",
                   "removing the previous version's files")
    else:
        info(f"Creating a new Zenodo record for {memo_id}.v{number}...")
        deposition = zenodo("POST", f"{ZENODO_API}/deposit/depositions", "creating the deposition", json={})

    bucket = (deposition.get("links") or {}).get("bucket")
    if not bucket:
        fail("Zenodo did not return an upload location for the deposition.")
    for path, name in uploads:
        info(f"Uploading {name}...")
        with path.open("rb") as fh:
            zenodo("PUT", f"{bucket}/{quote(name)}", f"uploading {name}",
                   data=fh, headers={"Content-Type": "application/octet-stream"})

    zenodo("PUT", f"{ZENODO_API}/deposit/depositions/{deposition['id']}", "setting the metadata",
           json={"metadata": zenodo_metadata(meta, memo_id, number, today)})
    info("Publishing on Zenodo...")
    published = zenodo("POST", f"{ZENODO_API}/deposit/depositions/{deposition['id']}/actions/publish",
                       "publishing the record")
    record_id = published.get("record_id") or published.get("id") or deposition["id"]
    links = published.get("links") or {}
    doi = extract_doi(published)
    concept_doi = extract_doi(published, concept=True)
    if not doi or not concept_doi:
        # Some Zenodo responses do not include the DOIs: ask Zenodo again
        for url in (f"{ZENODO_API}/deposit/depositions/{deposition['id']}", f"{ZENODO_API}/records/{record_id}"):
            data = lookup(url)
            doi = doi or extract_doi(data)
            concept_doi = concept_doi or extract_doi(data, concept=True)
    if not doi:
        info("  Warning: Zenodo did not report the DOI of this version; check the Zenodo record.")
    return {
        "doi": doi,
        "concept_doi": concept_doi,
        "zenodo_record": record_id,
        "record_url": links.get("record_html") or f"{ZENODO_SITE}/records/{record_id}",
    }


# ------------------------------------------------------- public memo page

def write_memo_readme(memo_dir, memo_id, meta):
    versions = []
    for vdir in memo_dir.iterdir():
        number = version_number(vdir.name) if vdir.is_dir() else None
        if number is not None and (vdir / "memo.yaml").is_file():
            vmeta = yaml.safe_load((vdir / "memo.yaml").read_text(encoding="utf-8")) or {}
            pdfs = sorted(p.name for p in vdir.iterdir() if p.suffix.lower() == ".pdf")
            versions.append((number, vdir.name, vmeta, pdfs))
    versions.sort(key=lambda v: v[0], reverse=True)

    authors = ", ".join(
        str(a.get("name", "")).strip() for a in meta.get("authors") or [] if isinstance(a, dict)
    )
    lines = [
        f"# {memo_id}: {str(meta.get('title', '')).strip()}",
        "",
        f"**Authors:** {authors}  ",
        f"**Series:** {meta.get('series') or ''}  ",
        f"**Latest version:** {memo_id}.v{versions[0][0]}  " if versions else "",
    ]
    if meta.get("concept_doi"):
        lines.append(f"**Cite all versions (concept DOI):** {meta['concept_doi']}  ")
    lines += ["", "## Abstract", "", "\n\n".join(paragraphs(meta.get("abstract"))), "", "## Versions", ""]
    for number, folder, vmeta, pdfs in versions:
        files = ", ".join(f"[{p}]({folder}/{quote(p)})" for p in pdfs) or "no PDF"
        record = vmeta.get("zenodo_record")
        zenodo_link = f" · [Zenodo record]({ZENODO_SITE}/records/{record})" if record else ""
        lines.append(f"- **{memo_id}.v{number}** ({vmeta.get('publication_date', '')}) · "
                     f"DOI: {vmeta.get('doi', '')}{zenodo_link} · {files}")
    lines += ["", "> Demo: DOIs were issued by Zenodo Sandbox and do not resolve via doi.org."]
    (memo_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- main

def main():
    if not SUBMISSION:
        fail("The submission number is missing.")
    info(f"Publishing memo from submission repository #{SUBMISSION}")
    meta_text, meta = load_metadata()
    files = files_to_publish()
    requested = version_number(meta.get("version"))
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    head_commit = subprocess.run(["git", "-C", str(MEMO_DIR), "rev-parse", "HEAD"],
                                 check=True, capture_output=True, text=True).stdout.strip()

    # What has already been published for this submission (from the public repository)
    public_id, history = published_history()
    last_number, last_meta = history[-1] if history else (0, {})
    previous_record = last_meta.get("zenodo_record")

    # Details recorded in memo.yaml right after an earlier Zenodo publication. Used only to
    # finish a publication whose public copy is missing, so that Zenodo is never published twice.
    recorded = version_number(meta.get("published_version"))
    recorded_id = str(meta.get("id") or "").strip()
    recovering = (recorded == requested == last_number + 1 and bool(meta.get("zenodo_record"))
                  and (public_id or ID_PATTERN.fullmatch(recorded_id)))

    if public_id:
        memo_id = public_id
        if recorded_id and recorded_id != memo_id:
            info(f"  Note: memo.yaml had a different id; the memo keeps its ID {memo_id}.")
    elif recovering:
        memo_id = recorded_id
    else:
        memo_id = new_memo_id(meta)
    label = f"{memo_id}.v{requested}"
    info(f"Memo {label} (latest published version: {f'{memo_id}.v{last_number}' if history else 'none'})")

    if requested == last_number:
        status = "unchanged"
        info(f"{label} has already been published; nothing new to publish.")
        result = {
            "doi": str(last_meta.get("doi") or ""),
            "concept_doi": str(last_meta.get("concept_doi") or ""),
            "zenodo_record": last_meta.get("zenodo_record"),
            "record_url": f"{ZENODO_SITE}/records/{last_meta.get('zenodo_record')}",
        }
        publication_date = str(last_meta.get("publication_date") or today)
        published_commit = str(last_meta.get("published_commit") or "")
    elif requested != last_number + 1:
        if history:
            fail(f"The latest published version is {memo_id}.v{last_number}, so the next version must be "
                 f"{last_number + 1}. Please set version: {last_number + 1} in memo.yaml (it is {requested}).")
        fail(f"A memo's first published version must be 1. Please set version: 1 in memo.yaml (it is {requested}).")
    elif recovering:
        status = "published"
        info(f"{label} is already on Zenodo but its public copy is missing; completing the publication.")
        record = meta.get("zenodo_record")
        result = {
            "doi": str(meta.get("doi") or ""),
            "concept_doi": str(meta.get("concept_doi") or ""),
            "zenodo_record": record,
            "record_url": f"{ZENODO_SITE}/records/{record}",
        }
        publication_date = str(meta.get("publication_date") or today)
        published_commit = str(meta.get("published_commit") or "")
    else:
        status = "published"
        uploads = published_names(files, label)
        result = publish_on_zenodo(meta, memo_id, requested, uploads, today, previous_record)
        publication_date = today
        published_commit = head_commit
        info(f"Published on Zenodo: DOI {result['doi']} (from commit {published_commit[:7]})")

    # 1. Record the publication in the private repository first, so that a retry never publishes twice.
    #    This also restores these fields if memo.yaml was edited by hand.
    fields = [
        ("id", json.dumps(memo_id)),
        ("submission_request", SUBMISSION),
        ("status", "published"),
        ("publication_date", json.dumps(publication_date)),
        ("doi", json.dumps(result["doi"])),
        ("concept_doi", json.dumps(result["concept_doi"])),
        ("zenodo_record", str(result["zenodo_record"])),
        ("published_version", str(requested)),
    ]
    if published_commit:
        fields.append(("published_commit", json.dumps(published_commit)))
    updated = meta_text
    for key, value in fields:
        updated = set_field(updated, key, value)
    info("Recording the publication in the submission repository...")
    (MEMO_DIR / "memo.yaml").write_text(updated, encoding="utf-8")
    try:
        commit_and_push(MEMO_DIR, f"Record publication of {label}")
    except subprocess.CalledProcessError:
        fail(f"{label} was published on Zenodo (record {result['zenodo_record']}, DOI {result['doi']}), "
             "but memo.yaml could not be updated in the submission repository. "
             "Add these values to memo.yaml by hand before retrying.")

    # 2. Copy only the publish/ files and the metadata to the public repository.
    #    A version that is already in the public repository is never modified.
    memo_dir = MEMOS / memo_id
    version_dir = memo_dir / f"v{requested}"
    if version_dir.exists():
        info("The public copy of this version already exists; it is left unchanged.")
    else:
        info("Copying the published files to the public repository...")
        version_dir.mkdir(parents=True)
        for path, name in published_names(files, label):
            shutil.copy2(path, version_dir / name)
        (version_dir / "memo.yaml").write_text(updated, encoding="utf-8")
        write_memo_readme(memo_dir, memo_id, yaml.safe_load(updated))
        commit_and_push(PUBLIC_DIR, f"Publish {label}")

    set_output("status", status)
    set_output("memo_id", memo_id)
    set_output("version", str(requested))
    set_output("label", label)
    set_output("doi", result["doi"])
    set_output("record_url", result["record_url"])
    set_output("public_url", f"{PUBLIC_REPO_URL}/tree/main/memos/{memo_id}")
    set_output("commit", published_commit)
    set_output("commit_url", f"{MEMO_REPO_URL}/commit/{published_commit}" if MEMO_REPO_URL and published_commit else "")
    info("Done.")


if __name__ == "__main__":
    main()
