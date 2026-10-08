#!/usr/bin/env python3
"""Publish an accepted memo from its private submission repository.

Only the files in the memo's publish/ folder are uploaded to Zenodo and
copied to the public memos repository. Drafts, other files and the review
discussion never leave the private repository.

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
SUBMISSION = os.environ.get("SUBMISSION_NUMBER", "?")
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
    if is_placeholder(meta.get("version")):
        problems.append("version")
    if problems:
        fail("memo.yaml is incomplete. Please fill in: " + ", ".join(problems) + ".")
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


def memo_id_for(meta):
    existing = meta.get("id")
    if existing:
        memo_id = str(existing).strip()
        if not ID_PATTERN.fullmatch(memo_id):
            fail("The id field in memo.yaml does not have the expected format (e.g. SV-2026-001).")
        return memo_id
    prefix = SERIES_PREFIXES.get(str(meta.get("series") or "").strip(), "MEMO")
    year = dt.datetime.now(dt.timezone.utc).year
    pattern = re.compile(rf"{prefix}-{year}-(\d{{3}})")
    numbers = [
        int(m.group(1))
        for d in (PUBLIC_DIR / "memos").glob("*")
        if (m := pattern.fullmatch(d.name))
    ]
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


def zenodo_metadata(meta, memo_id, version, today):
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
        "version": version,
        "publication_date": today,
        "notes": f"SKAO Memo Series (demo). Memo {memo_id}, version {version}. Fictitious test content.",
    }
    keywords = [str(k).strip() for k in (meta.get("keywords") or []) if str(k).strip()]
    if keywords:
        data["keywords"] = keywords
    if ZENODO_COMMUNITY:
        data["communities"] = [{"identifier": ZENODO_COMMUNITY}]
    return data


def publish_on_zenodo(meta, memo_id, version, files, today):
    previous = meta.get("zenodo_record")
    if previous:
        info(f"Creating version {version} of Zenodo record {previous}...")
        result = zenodo("POST", f"{ZENODO_API}/deposit/depositions/{previous}/actions/newversion",
                        "creating a new version")
        draft_url = (result.get("links") or {}).get("latest_draft")
        if not draft_url:
            fail("Zenodo did not return a draft for the new version.")
        deposition = zenodo("GET", draft_url, "reading the new version draft")
        for old in deposition.get("files") or []:
            zenodo("DELETE", f"{ZENODO_API}/deposit/depositions/{deposition['id']}/files/{old['id']}",
                   "removing the previous version's files")
    else:
        info("Creating a new Zenodo deposition...")
        deposition = zenodo("POST", f"{ZENODO_API}/deposit/depositions", "creating the deposition", json={})

    bucket = (deposition.get("links") or {}).get("bucket")
    if not bucket:
        fail("Zenodo did not return an upload location for the deposition.")
    for path in files:
        info(f"Uploading {path.name}...")
        with path.open("rb") as fh:
            zenodo("PUT", f"{bucket}/{quote(path.name)}", f"uploading {path.name}",
                   data=fh, headers={"Content-Type": "application/octet-stream"})

    zenodo("PUT", f"{ZENODO_API}/deposit/depositions/{deposition['id']}", "setting the metadata",
           json={"metadata": zenodo_metadata(meta, memo_id, version, today)})
    info("Publishing on Zenodo...")
    published = zenodo("POST", f"{ZENODO_API}/deposit/depositions/{deposition['id']}/actions/publish",
                       "publishing the record")
    record_id = published.get("record_id") or published.get("id") or deposition["id"]
    links = published.get("links") or {}
    return {
        "doi": published.get("doi") or "",
        "concept_doi": published.get("conceptdoi") or meta.get("concept_doi") or "",
        "zenodo_record": record_id,
        "record_url": links.get("record_html") or f"{ZENODO_SITE}/records/{record_id}",
    }


# ------------------------------------------------------- public memo page

def version_key(name):
    return [int(n) for n in re.findall(r"\d+", name)]


def write_memo_readme(memo_dir, memo_id, meta):
    versions = []
    for vdir in memo_dir.iterdir():
        if vdir.is_dir() and vdir.name.startswith("v") and (vdir / "memo.yaml").is_file():
            vmeta = yaml.safe_load((vdir / "memo.yaml").read_text(encoding="utf-8")) or {}
            pdfs = sorted(p.name for p in vdir.iterdir() if p.suffix.lower() == ".pdf")
            versions.append((vdir.name, vmeta, pdfs))
    versions.sort(key=lambda v: version_key(v[0]), reverse=True)

    authors = ", ".join(
        str(a.get("name", "")).strip() for a in meta.get("authors") or [] if isinstance(a, dict)
    )
    lines = [
        f"# {memo_id}: {str(meta.get('title', '')).strip()}",
        "",
        f"**Authors:** {authors}  ",
        f"**Series:** {meta.get('series') or ''}  ",
    ]
    if meta.get("concept_doi"):
        lines.append(f"**Cite all versions (concept DOI):** {meta['concept_doi']}  ")
    lines += ["", "## Abstract", "", "\n\n".join(paragraphs(meta.get("abstract"))), "", "## Versions", ""]
    for name, vmeta, pdfs in versions:
        files = ", ".join(f"[{p}]({name}/{quote(p)})" for p in pdfs) or "no PDF"
        record = vmeta.get("zenodo_record")
        zenodo_link = f" · [Zenodo record]({ZENODO_SITE}/records/{record})" if record else ""
        lines.append(f"- **{name}** ({vmeta.get('publication_date', '')}) · DOI: {vmeta.get('doi', '')}"
                     f"{zenodo_link} · {files}")
    lines += ["", "> Demo: DOIs were issued by Zenodo Sandbox and do not resolve via doi.org."]
    (memo_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- main

def main():
    info(f"Publishing memo from submission repository #{SUBMISSION}")
    meta_text, meta = load_metadata()
    files = files_to_publish()
    version = str(meta["version"]).strip()
    memo_id = memo_id_for(meta)
    info(f"Memo ID: {memo_id}, version {version}")

    memo_dir = PUBLIC_DIR / "memos" / memo_id
    version_dir = memo_dir / f"v{version}"
    public_copy_exists = version_dir.exists()
    published_version = meta.get("published_version")
    already_on_zenodo = bool(meta.get("doi")) and published_version is not None \
        and str(published_version).strip() == version
    if public_copy_exists and not already_on_zenodo:
        fail(f"Version {version} of {memo_id} has already been published. "
             "Increase 'version' in memo.yaml to publish a new version.")

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if already_on_zenodo:
        info("This version is already on Zenodo; it will not be published again.")
        record = meta.get("zenodo_record")
        result = {
            "doi": str(meta.get("doi")),
            "concept_doi": str(meta.get("concept_doi") or ""),
            "zenodo_record": record,
            "record_url": f"{ZENODO_SITE}/records/{record}",
        }
        publication_date = str(meta.get("publication_date") or today)
    else:
        result = publish_on_zenodo(meta, memo_id, version, files, today)
        publication_date = today
        info(f"Published on Zenodo: DOI {result['doi']}")

    updated = meta_text
    for key, value in [
        ("id", json.dumps(memo_id)),
        ("status", "published"),
        ("publication_date", json.dumps(publication_date)),
        ("doi", json.dumps(result["doi"])),
        ("concept_doi", json.dumps(result["concept_doi"])),
        ("zenodo_record", str(result["zenodo_record"])),
        ("published_version", json.dumps(version)),
    ]:
        updated = set_field(updated, key, value)

    # 1. Record the publication in the private repository first, so that a retry never publishes twice
    info("Recording the publication in the submission repository...")
    (MEMO_DIR / "memo.yaml").write_text(updated, encoding="utf-8")
    try:
        commit_and_push(MEMO_DIR, f"Record publication of {memo_id} v{version}")
    except subprocess.CalledProcessError:
        fail(f"The memo was published on Zenodo (record {result['zenodo_record']}, DOI {result['doi']}), "
             "but memo.yaml could not be updated in the submission repository. "
             "Add these values to memo.yaml by hand before retrying.")

    # 2. Copy only the publish/ files and the metadata to the public repository.
    #    A version that is already in the public repository is never modified.
    if public_copy_exists:
        info("The public copy of this version already exists; it is left unchanged.")
    else:
        info("Copying the published files to the public repository...")
        version_dir.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy2(path, version_dir / path.name)
        (version_dir / "memo.yaml").write_text(updated, encoding="utf-8")
        write_memo_readme(memo_dir, memo_id, yaml.safe_load(updated))
        commit_and_push(PUBLIC_DIR, f"Publish {memo_id} v{version}")

    set_output("memo_id", memo_id)
    set_output("version", version)
    set_output("doi", result["doi"])
    set_output("record_url", result["record_url"])
    set_output("public_url", f"{PUBLIC_REPO_URL}/tree/main/memos/{memo_id}")
    info("Done.")


if __name__ == "__main__":
    main()
