from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def defaults() -> tuple[dict[str, Any], dict[str, Any]]:
    profile = {
        "schema_version": 1,
        "candidate_id": None,
        "legal_name": None,
        "preferred_name": None,
        "email": None,
        "phone": None,
        "location": {"city": None, "region": None, "country": None},
        "work_authorization": {"country": None, "authorized": None, "needs_sponsorship": None},
        "resume": {"path": "private/resume.pdf"},
        "confirmed_by_user_at": None,
    }
    preferences = {
        "schema_version": 1,
        "search": {
            "role_families": [],
            "seniority": ["entry_level", "new_grad"],
            "max_required_years_experience": None,
            "graduation_window": None,
            "locations": [],
            "work_arrangements": [],
            "remote_eligible_regions": [],
            "excluded_companies": [],
            "enabled_sources": [],
            "max_roles_to_review_per_run": 10,
        },
        "applications": {
            "require_exact_role_approval": True,
            "narrative_answer_policy": "review_each",
            "unknown_submission_retry": "never_automatic",
            "max_concurrent_applications": 1,
        },
    }
    return profile, preferences


def private_paths(root: Path) -> dict[str, Path]:
    private = root / "private"
    return {
        "private": private,
        "profile": private / "config" / "profile.json",
        "preferences": private / "config" / "preferences.json",
        "answers": private / "config" / "answers.md",
        "resume": private / "resume.pdf",
        "database": private / "state" / "roles.sqlite3",
        "exports": private / "exports",
    }


def initialize(root: Path) -> None:
    paths = private_paths(root)
    paths["exports"].mkdir(parents=True, exist_ok=True)
    paths["profile"].parent.mkdir(parents=True, exist_ok=True)
    profile, preferences = defaults()
    for key, value in (("profile", profile), ("preferences", preferences)):
        path = paths[key]
        if not path.exists():
            path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    if not paths["answers"].exists():
        paths["answers"].write_text(
            "# Confirmed facts and reusable answers\n\n"
            "Add only facts you have confirmed. Leave unknown values blank; do not infer them.\n",
            encoding="utf-8",
        )
    paths["database"].parent.mkdir(parents=True, exist_ok=True)
    db = connect(paths["database"])
    db.close()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            location TEXT NOT NULL,
            work_arrangement TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            provider TEXT NOT NULL,
            tenant TEXT NOT NULL,
            requisition_id TEXT,
            description TEXT NOT NULL,
            fit_evidence TEXT NOT NULL,
            blockers TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS jobs_url ON jobs(canonical_url);
        CREATE TABLE IF NOT EXISTS observations (
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY(job_id, source, source_url)
        );
        CREATE TABLE IF NOT EXISTS decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            status TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            resume_sha256 TEXT,
            answer_policy TEXT,
            evidence TEXT NOT NULL,
            destination_url TEXT,
            approved_job_url TEXT,
            candidate_context_sha256 TEXT,
            approved_job_sha256 TEXT
        );
        CREATE TABLE IF NOT EXISTS source_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            result TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            approval_decision_id INTEGER NOT NULL REFERENCES decisions(decision_id),
            state TEXT NOT NULL,
            destination_url TEXT NOT NULL,
            resume_sha256 TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_stage TEXT,
            blocker TEXT,
            resolution TEXT,
            reason TEXT,
            evidence TEXT,
            evidence_source TEXT
        );
        CREATE INDEX IF NOT EXISTS attempts_job ON attempts(job_id, started_at);
        CREATE TABLE IF NOT EXISTS attempt_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
            from_state TEXT,
            to_state TEXT NOT NULL,
            event_at TEXT NOT NULL,
            stage TEXT,
            blocker TEXT,
            resolution TEXT,
            evidence TEXT,
            evidence_source TEXT
        );
        """
    )
    decision_columns = {row["name"] for row in db.execute("PRAGMA table_info(decisions)")}
    if "destination_url" not in decision_columns:
        db.execute("ALTER TABLE decisions ADD COLUMN destination_url TEXT")
    if "approved_job_url" not in decision_columns:
        db.execute("ALTER TABLE decisions ADD COLUMN approved_job_url TEXT")
    if "candidate_context_sha256" not in decision_columns:
        db.execute("ALTER TABLE decisions ADD COLUMN candidate_context_sha256 TEXT")
    if "approved_job_sha256" not in decision_columns:
        db.execute("ALTER TABLE decisions ADD COLUMN approved_job_sha256 TEXT")
    return db


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing {path}. Run the init command first.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def inspect_config(root: Path) -> dict[str, Any]:
    paths = private_paths(root)
    profile = read_json(paths["profile"])
    preferences = read_json(paths["preferences"])
    resume_value = profile.get("resume", {}).get("path")
    if not isinstance(resume_value, str) or not resume_value.strip():
        raise ValueError("profile.resume.path must name the single active resume")
    resume_path = (root / resume_value).resolve()
    if root.resolve() not in resume_path.parents:
        raise ValueError("The resume path must be inside the project root")
    resume_hash = hashlib.sha256(resume_path.read_bytes()).hexdigest() if resume_path.is_file() else None
    answers_path = paths["answers"]
    context_payload = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if answers_path.is_file():
        context_payload += b"\0" + answers_path.read_bytes()
    context_hash = hashlib.sha256(context_payload).hexdigest()
    search = preferences.get("search", {})
    applications = preferences.get("applications", {})
    missing = []
    if not resume_hash:
        missing.append(f"Add the one unchanged resume at {resume_value}")
    if not profile.get("confirmed_by_user_at"):
        missing.append("Confirm the profile facts privately")
    if applications.get("narrative_answer_policy") not in ("review_each", "draft_from_approved_facts"):
        missing.append("Choose a valid narrative_answer_policy")
    return {
        "profile": str(paths["profile"].relative_to(root)),
        "preferences": str(paths["preferences"].relative_to(root)),
        "resume": resume_value,
        "resume_sha256": resume_hash,
        "candidate_context_sha256": context_hash,
        "sources_enabled": len(search.get("enabled_sources", [])),
        "missing": missing,
        "state": str(paths["database"].relative_to(root)),
        "exports": str(paths["exports"].relative_to(root)),
    }


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Job URLs must be credential-free HTTPS URLs")
    tracking = {"fbclid", "gclid", "source", "sourceid", "gh_src", "lever-source"}
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if not key.lower().startswith("utm_") and key.lower() not in tracking]
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/") or "/"
    host = parsed.hostname.lower()
    if parsed.port and parsed.port != 443:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(query), ""))


def infer_arrangement(location: str, value: str | None = None) -> str:
    text = f"{location} {value or ''}".lower()
    if "remote" in text or "work from home" in text:
        return "remote"
    if "hybrid" in text:
        return "hybrid"
    if location.strip():
        return "on_site"
    return "unknown"


def evaluate(job: dict[str, Any], profile: dict[str, Any], preferences: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    search = preferences.get("search", {})
    company = str(job.get("company", ""))
    title = str(job.get("title", ""))
    location = str(job.get("location", ""))
    description = str(job.get("description", ""))
    arrangement = str(job.get("work_arrangement") or infer_arrangement(location))
    blockers: list[str] = []
    evidence: list[str] = []
    if company.lower() in {str(item).lower() for item in search.get("excluded_companies", [])}:
        return False, evidence, ["Company is excluded by preferences"]
    families = [str(item).lower() for item in search.get("role_families", [])]
    if families:
        matched = [item for item in families if item in title.lower()]
        if not matched:
            return False, evidence, ["Title does not match configured role families"]
        evidence.append("Title matches " + ", ".join(matched))

    seniority = search.get("seniority", [])
    seniority_markers = {
        "entry_level": ("entry level", "entry-level", "early career", "junior"),
        "new_grad": ("new grad", "new graduate", "graduate role", "recent graduate"),
    }
    if seniority:
        lowered = title.lower()
        if not any(marker in lowered for level in seniority for marker in seniority_markers.get(level, ())):
            if not re.search(r"\b(i|1)\s*$", lowered):
                return False, evidence, ["Title does not indicate configured entry-level/new-grad seniority"]
        evidence.append("Title indicates entry-level or new-grad seniority")

    arrangements = [str(item).lower() for item in search.get("work_arrangements", [])]
    if arrangements and arrangement != "unknown" and arrangement not in arrangements:
        return False, evidence, [f"Work arrangement {arrangement} is outside configured preferences"]
    if arrangements and arrangement == "unknown":
        blockers.append("Work arrangement is unknown")
    if arrangement != "unknown":
        evidence.append(f"Work arrangement: {arrangement}")

    locations = [str(item).lower() for item in search.get("locations", [])]
    if locations:
        searchable_location = location.lower()
        matched = [item for item in locations if item in searchable_location]
        location_unknown = not location or searchable_location in {"unknown", "n/a", "unspecified"}
        if not matched and location and not location_unknown:
            if arrangement == "remote":
                remote_regions = [str(item).lower() for item in search.get("remote_eligible_regions", [])]
                candidate_country = str(profile.get("location", {}).get("country") or "").lower()
                allowed_region = any(region in candidate_country or candidate_country in region for region in remote_regions if candidate_country)
                if not allowed_region:
                    blockers.append("Remote-region eligibility needs confirmation")
                else:
                    evidence.append("Remote region matches configured eligibility")
            else:
                return False, evidence, ["Location is outside configured preferences"]
        elif matched:
            evidence.append("Location matches " + ", ".join(matched))
        elif location_unknown:
            blockers.append("Location is missing; geographic fit is unknown")

    maximum = search.get("max_required_years_experience")
    for line in description.splitlines():
        if re.search(r"preferred|preferably|nice to have|bonus", line, re.IGNORECASE):
            continue
        range_match = re.search(
            r"\b(\d+)\s*(?:[-–—]|to|through)\s*(\d+)\s*\+?\s*(?:years?|yrs?)\b",
            line,
            re.IGNORECASE,
        )
        years_match = range_match or re.search(r"\b(\d+)\s*\+?\s*(?:years?|yrs?)\b", line, re.IGNORECASE)
        requirement = re.search(
            r"\b(required|minimum|must have)\b|(?:years?|yrs?).{0,24}\bexperience\b|\bexperience\b.{0,24}(?:years?|yrs?)",
            line,
            re.IGNORECASE,
        )
        if not years_match or not requirement:
            continue
        minimum_years = int(years_match.group(1))
        statement = range_match.group(0) if range_match else f"{minimum_years} years"
        if isinstance(maximum, int) and minimum_years > maximum:
            return False, evidence, [f"Requires {statement} of experience; minimum exceeds configured maximum of {maximum} years"]
        if isinstance(maximum, int):
            evidence.append(f"Required experience: {statement}; minimum is within configured maximum of {maximum} years")
        else:
            blockers.append(f"Required experience is stated ({statement}); no experience threshold is configured")

    graduation_window = search.get("graduation_window")
    if isinstance(graduation_window, dict):
        start_year = graduation_window.get("start_year")
        end_year = graduation_window.get("end_year")
        if isinstance(start_year, int) and isinstance(end_year, int) and start_year <= end_year:
            cohorts = re.findall(
                r"(?:class of|graduat(?:e|ing|ed)\s+(?:in\s+)?)(?:\s*)(20\d{2})(?:\s*(?:-|to|through)\s*(20\d{2}))?",
                description,
                re.IGNORECASE,
            )
            for first, last in cohorts:
                cohort_start = int(first)
                cohort_end = int(last or first)
                if cohort_end < start_year or cohort_start > end_year:
                    return False, evidence, [f"Posting targets graduation cohort {first}-{last or first}, outside configured window {start_year}-{end_year}"]
                evidence.append(f"Graduation cohort overlaps configured window {start_year}-{end_year}")

    authorization_text = description.lower()
    if any(phrase in authorization_text for phrase in ("authorized to work", "work authorization", "visa sponsorship")):
        blockers.append("Work-authorization terms need human review against confirmed profile")
    if not blockers and not evidence:
        blockers.append("Fit needs review; source did not provide enough evidence")
    return True, evidence, blockers


def canonical_record(raw: dict[str, Any], provider: str, tenant: str, source_url: str | None = None) -> dict[str, Any]:
    company = str(raw.get("company") or "").strip()
    title = str(raw.get("title") or "").strip()
    url = normalize_url(str(raw.get("url") or raw.get("canonical_url") or ""))
    if not company or not title:
        raise ValueError("Each role needs non-empty company and title fields")
    requisition_id = str(raw.get("requisition_id") or raw.get("id") or "").strip() or None
    location = str(raw.get("location") or "Unknown").strip()
    arrangement = str(raw.get("work_arrangement") or infer_arrangement(location, raw.get("workplace_type")))
    return {
        "company": company, "title": title, "location": location,
        "work_arrangement": arrangement, "canonical_url": url, "provider": provider,
        "tenant": tenant, "requisition_id": requisition_id,
        "description": str(raw.get("description") or ""),
        "source_url": normalize_url(source_url or url),
    }


def stable_id(record: dict[str, Any], db: sqlite3.Connection) -> str:
    req = record["requisition_id"]
    if req:
        found = db.execute(
            "SELECT job_id FROM jobs WHERE provider=? AND tenant=? AND requisition_id=?",
            (record["provider"], record["tenant"], req),
        ).fetchone()
        if found:
            return str(found["job_id"])
    found = db.execute(
        "SELECT job_id, requisition_id, provider, tenant FROM jobs WHERE canonical_url=?",
        (record["canonical_url"],),
    ).fetchall()
    conflicting_scope = any(
        row["provider"] == record["provider"] and row["tenant"] == record["tenant"]
        and req and row["requisition_id"] and row["requisition_id"] != req
        for row in found
    )
    if not conflicting_scope:
        for row in found:
            return str(row["job_id"])
    identity = f"{record['provider']}:{record['tenant']}:{req}" if req else record["canonical_url"]
    return f"J-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12].upper()}"


def save_record(db: sqlite3.Connection, raw: dict[str, Any], provider: str, tenant: str,
                profile: dict[str, Any], preferences: dict[str, Any], source_url: str | None = None) -> tuple[str, bool]:
    record = canonical_record(raw, provider, tenant, source_url)
    include, evidence, blockers = evaluate(record, profile, preferences)
    if not include:
        return "", False
    job_id = stable_id(record, db)
    timestamp = now()
    db.execute(
        """INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET company=excluded.company, title=excluded.title,
        location=excluded.location, work_arrangement=excluded.work_arrangement,
        canonical_url=excluded.canonical_url, description=excluded.description,
        fit_evidence=excluded.fit_evidence, blockers=excluded.blockers, last_seen=excluded.last_seen""",
        (job_id, record["company"], record["title"], record["location"], record["work_arrangement"],
         record["canonical_url"], provider, tenant, record["requisition_id"], record["description"],
         json.dumps(evidence), json.dumps(blockers), timestamp, timestamp),
    )
    source_identity = f"{provider}:{tenant}:{record['requisition_id'] or 'url'}"
    db.execute("INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?)",
               (job_id, source_identity, record["source_url"], timestamp))
    return job_id, True


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_host: str):
        super().__init__()
        self.allowed_host = allowed_host

    def redirect_request(self, request: Any, response: Any, code: int, message: str,
                         headers: Any, new_url: str) -> Any:
        parsed = urllib.parse.urlsplit(new_url)
        if parsed.scheme != "https" or parsed.hostname != self.allowed_host:
            raise urllib.error.URLError("Refused redirect outside the configured public source host")
        return super().redirect_request(request, response, code, message, headers, new_url)


def fetch_json(url: str, allowed_host: str) -> Any:
    opener = urllib.request.build_opener(SameHostRedirect(allowed_host))
    request = urllib.request.Request(url, headers={"User-Agent": "MuseRoleHelper/1.0", "Accept": "application/json"})
    with opener.open(request, timeout=20) as response:
        if urllib.parse.urlsplit(response.geturl()).hostname != allowed_host:
            raise ValueError("Source redirected outside its configured host")
        return json.loads(response.read().decode("utf-8"))


def discover_source(source: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    kind = str(source.get("type", "")).lower()
    tenant = str(source.get("tenant", "")).strip()
    company = str(source.get("company", tenant)).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", tenant):
        raise ValueError("Source tenant must be a Greenhouse board token or Lever site name")
    if kind == "greenhouse":
        host = "boards-api.greenhouse.io"
        url = f"https://{host}/v1/boards/{urllib.parse.quote(tenant)}/jobs?content=true"
        payload = fetch_json(url, host)
        rows = payload.get("jobs", [])
        jobs = [{
            "id": row.get("id"), "company": company, "title": row.get("title"),
            "location": (row.get("location") or {}).get("name", "Unknown"),
            "url": row.get("absolute_url"), "description": row.get("content", ""),
        } for row in rows]
    elif kind == "lever":
        host = "api.lever.co"
        url = f"https://{host}/v0/postings/{urllib.parse.quote(tenant)}?mode=json"
        payload = fetch_json(url, host)
        jobs = [{
            "id": row.get("id"), "company": company, "title": row.get("text"),
            "location": (row.get("categories") or {}).get("location", "Unknown"),
            "workplace_type": (row.get("categories") or {}).get("workplaceType"),
            "url": row.get("hostedUrl"), "description": row.get("descriptionPlain", ""),
        } for row in payload]
    else:
        raise ValueError(f"Unsupported source type {kind!r}; use greenhouse or lever")
    return jobs, kind, url


def current_decision(db: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM decisions WHERE job_id=? ORDER BY decision_id DESC LIMIT 1", (job_id,)).fetchone()


def latest_attempt(db: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM attempts WHERE job_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (job_id,),
    ).fetchone()


def job_context_hash(job: sqlite3.Row) -> str:
    values = {key: job[key] for key in (
        "company", "title", "location", "work_arrangement", "canonical_url", "description"
    )}
    payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def preflight(db: sqlite3.Connection, root: Path, job_id: str, destination: str | None = None,
              ignore_attempt_id: str | None = None, allow_closed: bool = False) -> dict[str, Any]:
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        raise ValueError(f"Unknown job ID {job_id}")
    errors: list[str] = []
    decision = current_decision(db, job_id)
    config: dict[str, Any] = {}
    try:
        config = inspect_config(root)
        errors.extend(config["missing"])
    except ValueError as exc:
        errors.append(str(exc))

    requested_destination = None
    if decision and decision["destination_url"]:
        requested_destination = normalize_url(destination or decision["destination_url"])
    elif destination:
        requested_destination = normalize_url(destination)
    else:
        requested_destination = normalize_url(str(job["canonical_url"]))

    if not decision or decision["status"] != "APPROVED":
        current = decision["status"] if decision else "PENDING"
        errors.append(f"Current exact-role decision is {current}, not APPROVED")
    else:
        if not decision["resume_sha256"] or decision["resume_sha256"] != config.get("resume_sha256"):
            errors.append("The active resume is missing or differs from the resume bound to approval")
        if not decision["candidate_context_sha256"] or decision["candidate_context_sha256"] != config.get("candidate_context_sha256"):
            errors.append("Confirmed candidate facts or reusable answers changed; review and approve this role again")
        if decision["answer_policy"] != read_json(private_paths(root)["preferences"]).get("applications", {}).get("narrative_answer_policy"):
            errors.append("Narrative answer policy changed; review and approve this role again")
        if not decision["approved_job_sha256"] or decision["approved_job_sha256"] != job_context_hash(job):
            errors.append("Material role details changed; review and approve the current posting again")
        approved_job_url = decision["approved_job_url"]
        if not approved_job_url or normalize_url(approved_job_url) != normalize_url(str(job["canonical_url"])):
            errors.append("The role URL changed or approval predates destination binding; approve the current role again")
        approved_destination = decision["destination_url"]
        if not approved_destination:
            errors.append("Approval predates destination binding; approve the role again")
        elif requested_destination != normalize_url(approved_destination):
            errors.append("Application destination differs from the destination bound to approval")

    prior = latest_attempt(db, job_id)
    if prior and prior["attempt_id"] != ignore_attempt_id:
        if prior["state"] != "CLOSED":
            errors.append(f"Attempt {prior['attempt_id']} is {prior['state']}; resume or reconcile it instead of starting another")
        elif not allow_closed:
            errors.append("The previous attempt is CLOSED; start a new attempt with --reason only after explicit user direction")
    review_items = json.loads(job["blockers"])
    state = "BLOCKED" if errors else "NEEDS_HUMAN" if review_items else "READY"
    return {
        "job_id": job_id,
        "status": state,
        "destination": requested_destination,
        "reasons": errors,
        "review_items": review_items,
        "prior_attempt_id": prior["attempt_id"] if prior else None,
    }


def append_attempt_event(db: sqlite3.Connection, attempt_id: str, from_state: str | None,
                         to_state: str, stage: str | None, blocker: str | None,
                         resolution: str | None, evidence: str | None,
                         evidence_source: str | None) -> None:
    db.execute(
        """INSERT INTO attempt_events(
        attempt_id, from_state, to_state, event_at, stage, blocker, resolution, evidence, evidence_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (attempt_id, from_state, to_state, now(), stage, blocker, resolution, evidence, evidence_source),
    )


def create_attempt(db: sqlite3.Connection, root: Path, job_id: str, destination: str | None,
                   reason: str | None) -> dict[str, Any]:
    db.execute("BEGIN IMMEDIATE")
    result = preflight(db, root, job_id, destination, allow_closed=bool(reason))
    if result["status"] == "BLOCKED":
        raise ValueError("Preflight blocked: " + "; ".join(result["reasons"]))
    prior = latest_attempt(db, job_id)
    if prior and prior["state"] == "CLOSED" and not reason:
        raise ValueError("A new attempt after CLOSED requires --reason with the user's explicit direction")
    decision = current_decision(db, job_id)
    attempt_id = uuid.uuid4().hex.upper()
    state = "NEEDS_HUMAN" if result["review_items"] else "IN_PROGRESS"
    timestamp = now()
    blocker = json.dumps(result["review_items"]) if result["review_items"] else None
    db.execute(
        """INSERT INTO attempts(
        attempt_id, job_id, approval_decision_id, state, destination_url, resume_sha256,
        started_at, updated_at, last_stage, blocker, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (attempt_id, job_id, decision["decision_id"], state, result["destination"],
         decision["resume_sha256"], timestamp, timestamp, "preflight", blocker, reason),
    )
    append_attempt_event(db, attempt_id, None, state, "preflight", blocker, None, None, None)
    db.commit()
    return {"attempt_id": attempt_id, "job_id": job_id, "state": state,
            "destination": result["destination"], "review_items": result["review_items"]}


ATTEMPT_TRANSITIONS = {
    "IN_PROGRESS": {"NEEDS_HUMAN", "SUBMITTING", "CLOSED"},
    "NEEDS_HUMAN": {"IN_PROGRESS", "APPLIED", "CLOSED"},
    "SUBMITTING": {"NEEDS_HUMAN", "APPLIED", "SUBMISSION_UNKNOWN"},
    "SUBMISSION_UNKNOWN": {"APPLIED", "CLOSED"},
    "APPLIED": set(),
    "CLOSED": set(),
}


def update_attempt(db: sqlite3.Connection, root: Path, attempt_id: str, state: str | None,
                   stage: str | None, blocker: str | None, resolution: str | None,
                   evidence: str | None, evidence_source: str | None) -> dict[str, Any]:
    db.execute("BEGIN IMMEDIATE")
    attempt = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
    if not attempt:
        raise ValueError(f"Unknown attempt ID {attempt_id}")
    previous = str(attempt["state"])
    target = state or previous
    if target != previous and target not in ATTEMPT_TRANSITIONS[previous]:
        raise ValueError(f"Invalid attempt transition {previous} -> {target}")
    if target == "NEEDS_HUMAN" and not (blocker or attempt["blocker"]):
        raise ValueError("NEEDS_HUMAN requires --blocker describing the actual issue")
    if previous == "NEEDS_HUMAN" and target == "IN_PROGRESS" and not (resolution and resolution.strip()):
        raise ValueError("Resuming needs --resolution describing how the blocker was addressed")
    if target in {"IN_PROGRESS", "SUBMITTING"}:
        check = preflight(db, root, str(attempt["job_id"]), str(attempt["destination_url"]),
                          ignore_attempt_id=attempt_id)
        if check["status"] == "BLOCKED":
            raise ValueError("Preflight blocked: " + "; ".join(check["reasons"]))
    if target == "APPLIED":
        if not evidence or not evidence.strip():
            raise ValueError("APPLIED requires job-specific confirmation evidence")
        if evidence_source not in {"job_confirmation", "user_reported"}:
            raise ValueError("APPLIED evidence source must be job_confirmation or user_reported")
    if target == "SUBMISSION_UNKNOWN" and (
        not evidence or evidence_source not in {"agent_observed", "user_reported"}
    ):
        raise ValueError("SUBMISSION_UNKNOWN requires evidence and its source; do not retry automatically")
    if target == "CLOSED" and not (evidence and evidence.strip()):
        raise ValueError("CLOSED requires a reason in --evidence")

    next_blocker = blocker if target == "NEEDS_HUMAN" else None if target == "IN_PROGRESS" else attempt["blocker"]
    next_resolution = resolution if resolution is not None else attempt["resolution"]
    next_stage = stage if stage is not None else attempt["last_stage"]
    next_evidence = evidence if evidence is not None else attempt["evidence"]
    next_source = evidence_source if evidence_source is not None else attempt["evidence_source"]
    timestamp = now()
    db.execute(
        """UPDATE attempts SET state=?, updated_at=?, last_stage=?, blocker=?, resolution=?,
        evidence=?, evidence_source=? WHERE attempt_id=?""",
        (target, timestamp, next_stage, next_blocker, next_resolution, next_evidence, next_source, attempt_id),
    )
    append_attempt_event(db, attempt_id, previous, target, next_stage, next_blocker,
                         next_resolution, next_evidence, next_source)
    db.commit()
    return {"attempt_id": attempt_id, "job_id": attempt["job_id"], "state": target,
            "updated_at": timestamp, "last_stage": next_stage, "blocker": next_blocker,
            "resolution": next_resolution, "evidence": next_evidence,
            "evidence_source": next_source}


def job_view(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    decision = current_decision(db, str(row["job_id"]))
    attempt = latest_attempt(db, str(row["job_id"]))
    sources = [item["source"] for item in db.execute(
        "SELECT DISTINCT source FROM observations WHERE job_id=? ORDER BY source", (row["job_id"],)
    )]
    possible_duplicates = []
    peers = db.execute(
        "SELECT job_id, provider, tenant, requisition_id, canonical_url, company, title, location FROM jobs WHERE job_id<>?",
        (row["job_id"],),
    ).fetchall()
    for peer in peers:
        same_details = all(
            str(peer[field]).strip().casefold() == str(row[field]).strip().casefold()
            for field in ("company", "title", "location")
        )
        known_distinct = (
            peer["provider"] == row["provider"] and peer["tenant"] == row["tenant"]
            and peer["requisition_id"] and row["requisition_id"]
            and peer["requisition_id"] != row["requisition_id"]
        )
        if same_details and peer["canonical_url"] != row["canonical_url"] and not known_distinct:
            possible_duplicates.append(str(peer["job_id"]))
    return {
        "job_id": row["job_id"], "company": row["company"], "title": row["title"],
        "location": row["location"], "work_arrangement": row["work_arrangement"],
        "url": row["canonical_url"], "fit_evidence": json.loads(row["fit_evidence"]),
        "blockers": json.loads(row["blockers"]), "sources": sources,
        "possible_duplicate_ids": sorted(possible_duplicates),
        "decision": decision["status"] if decision else "PENDING",
        "application_state": attempt["state"] if attempt else "NOT_STARTED",
        "attempt_id": attempt["attempt_id"] if attempt else None,
        "first_seen": row["first_seen"], "last_seen": row["last_seen"],
    }


def csv_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local discovery and review helpers for the Muse workflow")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root (defaults to current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create private config templates and SQLite state")
    commands.add_parser("inspect", help="Check private configuration and show resume hash")
    commands.add_parser("discover", help="Query configured Greenhouse and Lever boards")
    ingest = commands.add_parser("ingest", help="Normalize a Muse-observed public job record from JSON")
    ingest.add_argument("--file", type=Path, help="JSON file; omit to read one JSON object from stdin")
    listing = commands.add_parser("list", help="List filtered roles")
    listing.add_argument("--limit", type=int, default=100)
    show = commands.add_parser("show", help="Show one role and its source provenance")
    show.add_argument("job_id")
    decide = commands.add_parser("decide", help="Record an exact-ID approve, skip, defer, or revoke decision")
    decide.add_argument("job_id")
    decide.add_argument("decision", choices=("approve", "skip", "defer", "revoke"))
    decide.add_argument("--destination", help="Inspected application destination to bind to approval")
    preflight_command = commands.add_parser("preflight", help="Check exact approval, resume, destination, and prior attempts")
    preflight_command.add_argument("job_id")
    preflight_command.add_argument("--destination", help="Current inspected application destination")
    attempt_command = commands.add_parser("attempt", help="Start and update an application attempt")
    attempt_commands = attempt_command.add_subparsers(dest="attempt_command", required=True)
    attempt_start = attempt_commands.add_parser("start", help="Persist IN_PROGRESS or NEEDS_HUMAN after preflight")
    attempt_start.add_argument("job_id")
    attempt_start.add_argument("--destination", help="Inspected application destination")
    attempt_start.add_argument("--reason", help="Required to start after a prior CLOSED attempt")
    attempt_update = attempt_commands.add_parser("update", help="Persist an attempt stage or outcome")
    attempt_update.add_argument("attempt_id")
    attempt_update.add_argument("--state", choices=tuple(ATTEMPT_TRANSITIONS))
    attempt_update.add_argument("--stage")
    attempt_update.add_argument("--blocker")
    attempt_update.add_argument("--resolution")
    attempt_update.add_argument("--evidence")
    attempt_update.add_argument("--evidence-source", choices=("job_confirmation", "user_reported", "agent_observed"))
    attempt_show = attempt_commands.add_parser("show", help="Show attempt state and event history")
    attempt_show.add_argument("attempt_id")
    export = commands.add_parser("export", help="Export review and application-status CSV")
    export.add_argument("--output", type=Path, help="Output path; defaults to private/exports/roles.csv")
    return parser


def run(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    paths = private_paths(root)
    if args.command == "init":
        initialize(root)
        print(json.dumps({"initialized": True, "private": str(paths["private"].relative_to(root)), "state": str(paths["database"].relative_to(root))}))
        return 0
    if args.command == "inspect":
        print(json.dumps(inspect_config(root), indent=2))
        return 0

    profile = read_json(paths["profile"])
    preferences = read_json(paths["preferences"])
    db = connect(paths["database"])
    try:
        if args.command == "discover":
            sources = preferences.get("search", {}).get("enabled_sources", [])
            if not sources:
                raise ValueError("No sources configured; add Greenhouse or Lever boards to search.enabled_sources")
            result = []
            remaining = preferences.get("search", {}).get("max_roles_to_review_per_run", 10)
            if not isinstance(remaining, int) or remaining < 1:
                raise ValueError("search.max_roles_to_review_per_run must be a positive integer")
            sources_left = len(sources)
            for source in sources:
                started = now()
                label = f"{source.get('type', 'unknown')}:{source.get('tenant', 'unknown')}"
                quota = (remaining + sources_left - 1) // sources_left if remaining else 0
                sources_left -= 1
                try:
                    records, kind, source_url = discover_source(source)
                    saved = 0
                    considered = min(len(records), quota)
                    for record in records[:considered]:
                        job_id, included = save_record(db, record, kind, str(source["tenant"]), profile, preferences, source_url)
                        saved += int(included and bool(job_id))
                    remaining -= considered
                    db.execute("INSERT INTO source_runs(source, started_at, result, detail) VALUES (?, ?, ?, ?)",
                               (label, started, "OK", f"received={len(records)} considered={considered} saved={saved}"))
                    result.append({"source": label, "status": "OK", "received": len(records), "saved": saved})
                except (ValueError, KeyError, TypeError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
                    db.execute("INSERT INTO source_runs(source, started_at, result, detail) VALUES (?, ?, ?, ?)",
                               (label, started, "FAILED", str(exc)[:500]))
                    result.append({"source": label, "status": "FAILED", "error": str(exc)})
            db.commit()
            print(json.dumps(result, indent=2))
            return 0 if any(item["status"] == "OK" for item in result) else 1
        if args.command == "ingest":
            if args.file:
                input_path = args.file.resolve() if args.file.is_absolute() else (root / args.file).resolve()
                if root not in input_path.parents:
                    raise ValueError("Ingest JSON files must stay inside the project root")
                raw_text = input_path.read_text(encoding="utf-8")
            else:
                raw_text = sys.stdin.read()
            value = json.loads(raw_text)
            records = value if isinstance(value, list) else [value]
            saved = []
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("Each ingested role must be a JSON object")
                job_id, included = save_record(db, record, "muse", "observed", profile, preferences,
                                               str(record.get("source_url") or record.get("url") or ""))
                if included:
                    saved.append(job_id)
            db.commit()
            print(json.dumps({"saved": saved, "count": len(saved)}))
            return 0
        if args.command == "list":
            if args.limit < 1:
                raise ValueError("--limit must be positive")
            rows = db.execute("SELECT * FROM jobs ORDER BY last_seen DESC LIMIT ?", (args.limit,)).fetchall()
            print(json.dumps([job_view(db, row) for row in rows], indent=2))
            return 0
        if args.command == "show":
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (args.job_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown job ID {args.job_id}")
            print(json.dumps(job_view(db, row), indent=2))
            return 0
        if args.command == "preflight":
            result = preflight(db, root, args.job_id, args.destination)
            print(json.dumps(result, indent=2))
            return 2 if result["status"] == "BLOCKED" else 0
        if args.command == "attempt":
            if args.attempt_command == "start":
                result = create_attempt(db, root, args.job_id, args.destination, args.reason)
                print(json.dumps(result, indent=2))
                return 0
            if args.attempt_command == "update":
                result = update_attempt(db, root, args.attempt_id, args.state, args.stage,
                                        args.blocker, args.resolution, args.evidence,
                                        args.evidence_source)
                print(json.dumps(result, indent=2))
                return 0
            attempt = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (args.attempt_id,)).fetchone()
            if not attempt:
                raise ValueError(f"Unknown attempt ID {args.attempt_id}")
            events = [dict(event) for event in db.execute(
                "SELECT from_state, to_state, event_at, stage, blocker, resolution, evidence, evidence_source "
                "FROM attempt_events WHERE attempt_id=? ORDER BY event_id", (args.attempt_id,)
            )]
            print(json.dumps({"attempt": dict(attempt), "events": events}, indent=2))
            return 0
        if args.command == "decide":
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (args.job_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown job ID {args.job_id}")
            status = {"approve": "APPROVED", "skip": "SKIPPED", "defer": "DEFERRED", "revoke": "REVOKED"}[args.decision]
            resume_hash = None
            policy = None
            destination_url = None
            approved_job_url = None
            candidate_context_sha256 = None
            approved_job_sha256 = None
            if args.decision == "approve":
                config = inspect_config(root)
                if config["missing"]:
                    raise ValueError("Approval requires completed private setup: " + "; ".join(config["missing"]))
                resume_hash = config["resume_sha256"]
                policy = preferences.get("applications", {}).get("narrative_answer_policy")
                destination_url = normalize_url(args.destination or str(row["canonical_url"]))
                approved_job_url = normalize_url(str(row["canonical_url"]))
                candidate_context_sha256 = config["candidate_context_sha256"]
                approved_job_sha256 = job_context_hash(row)
            elif args.destination:
                raise ValueError("--destination is only valid with an approve decision")
            db.execute("""INSERT INTO decisions(
                job_id, status, decided_at, resume_sha256, answer_policy, evidence,
                destination_url, approved_job_url, candidate_context_sha256, approved_job_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (args.job_id, status, now(), resume_hash, policy,
                    "explicit command with exact job ID", destination_url, approved_job_url,
                    candidate_context_sha256, approved_job_sha256))
            db.commit()
            print(json.dumps({"job_id": args.job_id, "decision": status, "resume_sha256": resume_hash,
                              "answer_policy": policy, "destination": destination_url}))
            return 0
        if args.command == "export":
            output = args.output or paths["exports"] / "roles.csv"
            output = output.resolve()
            if paths["private"].resolve() not in output.parents:
                raise ValueError("CSV output must stay inside private/")
            output.parent.mkdir(parents=True, exist_ok=True)
            rows = db.execute("SELECT * FROM jobs ORDER BY company, title, job_id").fetchall()
            with output.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=("job_id", "company", "title", "location", "url", "decision", "application_state", "applied_at", "notes"))
                writer.writeheader()
                for row in rows:
                    view = job_view(db, row)
                    attempt = latest_attempt(db, str(row["job_id"]))
                    notes = view["blockers"]
                    if attempt:
                        notes.extend(value for value in (attempt["blocker"], attempt["resolution"], attempt["evidence"]) if value)
                    writer.writerow({
                        "job_id": csv_safe(view["job_id"]), "company": csv_safe(view["company"]),
                        "title": csv_safe(view["title"]), "location": csv_safe(view["location"]),
                        "url": csv_safe(view["url"]), "decision": csv_safe(view["decision"]),
                        "application_state": attempt["state"] if attempt else "NOT_STARTED",
                        "applied_at": attempt["updated_at"] if attempt and attempt["state"] == "APPLIED" else "",
                        "notes": csv_safe("; ".join(notes)),
                    })
            print(json.dumps({"exported": str(output.relative_to(root)), "rows": len(rows)}))
            return 0
    finally:
        db.close()
    return 2


def main() -> int:
    args = command_parser().parse_args()
    try:
        return run(args)
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())