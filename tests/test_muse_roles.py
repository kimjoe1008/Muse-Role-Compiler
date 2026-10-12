import argparse
import contextlib
import csv
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import muse_roles


class MuseRolesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        muse_roles.initialize(self.root)
        self.paths = muse_roles.private_paths(self.root)
        self.profile, self.preferences = muse_roles.defaults()
        self.preferences["search"]["seniority"] = ["entry_level", "new_grad"]

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, root, *arguments):
        command = [sys.executable, str(Path(muse_roles.__file__).resolve()), "--root", str(root), *arguments]
        return subprocess.run(command, text=True, capture_output=True)

    def approved_job(self, description="", destination=None):
        self.paths["resume"].write_bytes(b"synthetic resume")
        self.profile["confirmed_by_user_at"] = "2026-10-09"
        self.paths["profile"].write_text(json.dumps(self.profile), encoding="utf-8")
        self.paths["preferences"].write_text(json.dumps(self.preferences), encoding="utf-8")
        db = muse_roles.connect(self.paths["database"])
        raw = {"company": "Acme", "title": "Software Engineer I", "location": "Seattle",
               "url": "https://jobs.example/role", "id": "req-1", "description": description}
        job_id, _ = muse_roles.save_record(db, raw, "muse", "observed", self.profile, self.preferences)
        db.commit()
        db.close()
        args = ["decide", job_id, "approve"]
        if destination:
            args.extend(("--destination", destination))
        result = self.cli(self.root, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return job_id

    def test_initialize_preserves_existing_private_configuration(self):
        profile_path = self.paths["profile"]
        profile_path.write_text('{"private":"kept"}', encoding="utf-8")
        muse_roles.initialize(self.root)
        self.assertEqual(json.loads(profile_path.read_text(encoding="utf-8")), {"private": "kept"})

    def test_tracker_migration_preserves_decisions_and_adds_attempt_tables(self):
        database = self.paths["database"]
        db = sqlite3.connect(database)
        db.executescript(
            """
            DROP TABLE decisions;
            DROP TABLE jobs;
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL,
                location TEXT NOT NULL, work_arrangement TEXT NOT NULL, canonical_url TEXT NOT NULL,
                provider TEXT NOT NULL, tenant TEXT NOT NULL, requisition_id TEXT, description TEXT NOT NULL,
                fit_evidence TEXT NOT NULL, blockers TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                status TEXT NOT NULL, decided_at TEXT NOT NULL, resume_sha256 TEXT,
                answer_policy TEXT, evidence TEXT NOT NULL
            );
            INSERT INTO jobs VALUES ('J-OLD', 'Acme', 'Engineer', 'NY', 'on_site',
                'https://jobs.example/old', 'muse', 'observed', NULL, '', '[]', '[]', 't1', 't2');
            INSERT INTO decisions(job_id, status, decided_at, evidence)
                VALUES ('J-OLD', 'SKIPPED', 't2', 'historic choice');
            """
        )
        db.close()
        migrated = muse_roles.connect(database)
        decision = migrated.execute("SELECT status, evidence, destination_url, approved_job_url FROM decisions").fetchone()
        tables = {row["name"] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        migrated.close()
        self.assertEqual((decision["status"], decision["evidence"], decision["destination_url"],
                  decision["approved_job_url"]), ("SKIPPED", "historic choice", None, None))
        self.assertTrue({"attempts", "attempt_events"}.issubset(tables))

    def test_cli_init_and_inspect(self):
        root = self.root / "fresh"
        command = [sys.executable, str(Path(muse_roles.__file__).resolve()), "--root", str(root)]
        initialized = subprocess.run(command + ["init"], text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(initialized.stdout)["state"].replace("\\", "/"), "private/state/roles.sqlite3")
        inspected = subprocess.run(command + ["inspect"], text=True, capture_output=True, check=True)
        summary = json.loads(inspected.stdout)
        self.assertIsNone(summary["resume_sha256"])
        self.assertTrue(summary["missing"])

    def test_source_cli_add_is_idempotent_and_remove_preserves_other_preferences(self):
        result = self.cli(self.root, "source", "add", "ashby", "--tenant", "n1", "--company", "N1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["added"])

        repeated = self.cli(self.root, "source", "add", "ashby", "--tenant", "n1", "--company", "N1")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertTrue(json.loads(repeated.stdout)["already_configured"])

        preferences = json.loads(self.paths["preferences"].read_text(encoding="utf-8"))
        preferences["search"]["locations"] = ["New York"]
        self.paths["preferences"].write_text(json.dumps(preferences), encoding="utf-8")
        listed = self.cli(self.root, "source", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["sources"], [{"type": "ashby", "tenant": "n1", "company": "N1"}])

        removed = self.cli(self.root, "source", "remove", "ashby", "--tenant", "n1")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        updated = json.loads(self.paths["preferences"].read_text(encoding="utf-8"))
        self.assertEqual(updated["search"]["enabled_sources"], [])
        self.assertEqual(updated["search"]["locations"], ["New York"])

    def test_search_configure_updates_only_requested_fields_and_can_clear(self):
        self.cli(self.root, "source", "add", "ashby", "--tenant", "n1")
        configured = self.cli(
            self.root, "search", "configure", "--role-family", "Software Engineer",
            "--role-family", "Product Manager", "--location", "New York",
            "--work-arrangement", "hybrid", "--max-required-years", "0", "--max-roles", "25",
        )
        self.assertEqual(configured.returncode, 0, configured.stderr)
        preferences = json.loads(self.paths["preferences"].read_text(encoding="utf-8"))
        search = preferences["search"]
        self.assertEqual(search["role_families"], ["Software Engineer", "Product Manager"])
        self.assertEqual(search["locations"], ["New York"])
        self.assertEqual(search["work_arrangements"], ["hybrid"])
        self.assertEqual(search["max_required_years_experience"], 0)
        self.assertEqual(search["max_roles_to_review_per_run"], 25)
        self.assertEqual(search["enabled_sources"], [{"type": "ashby", "tenant": "n1", "company": "n1"}])
        self.assertEqual(search["seniority"], ["entry_level", "new_grad"])

        cleared = self.cli(self.root, "search", "configure", "--clear-role-families", "--clear-experience-limit")
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        updated = json.loads(self.paths["preferences"].read_text(encoding="utf-8"))["search"]
        self.assertEqual(updated["role_families"], [])
        self.assertIsNone(updated["max_required_years_experience"])
        self.assertEqual(updated["enabled_sources"], search["enabled_sources"])

    def test_url_normalization_removes_tracking_but_keeps_requisition(self):
        url = muse_roles.normalize_url("https://jobs.example/role/?utm_source=mail&gh_jid=123#apply")
        self.assertEqual(url, "https://jobs.example/role?gh_jid=123")

    def test_exact_url_deduplicates_sources_but_distinct_scoped_requisitions_survive(self):
        db = muse_roles.connect(self.paths["database"])
        raw = {"company": "Acme", "title": "Software Engineer I", "location": "Seattle, WA",
               "url": "https://jobs.example/role?utm_medium=feed", "description": ""}
        first, _ = muse_roles.save_record(db, {**raw, "id": "gh-1"}, "greenhouse", "acme",
                                          self.profile, self.preferences)
        repeated, _ = muse_roles.save_record(db, {**raw, "id": "gh-1"}, "greenhouse", "acme",
                             self.profile, self.preferences)
        duplicate, _ = muse_roles.save_record(db, {**raw, "id": "lev-1"}, "lever", "acme",
                                              self.profile, self.preferences)
        distinct, _ = muse_roles.save_record(db, {**raw, "id": "gh-2"}, "greenhouse", "acme",
                                             self.profile, self.preferences)
        self.assertEqual(first, duplicate)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, distinct)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM observations WHERE job_id=?", (first,)).fetchone()[0], 2)
        self.assertEqual(
            [row["source"] for row in db.execute("SELECT source FROM observations WHERE job_id=? ORDER BY source", (first,))],
            ["greenhouse:acme:gh-1", "lever:acme:lev-1"],
        )
        db.close()

    def test_lookalike_roles_are_flagged_without_merging(self):
        db = muse_roles.connect(self.paths["database"])
        base = {"company": "Acme", "title": "Software Engineer I", "location": "Seattle",
                "description": ""}
        first, _ = muse_roles.save_record(db, {**base, "id": "gh-1", "url": "https://jobs.example/1"},
                                          "greenhouse", "acme", self.profile, self.preferences)
        second, _ = muse_roles.save_record(db, {**base, "id": "lev-1", "url": "https://careers.example/role"},
                                           "lever", "acme", self.profile, self.preferences)
        self.assertNotEqual(first, second)
        first_row = db.execute("SELECT * FROM jobs WHERE job_id=?", (first,)).fetchone()
        second_row = db.execute("SELECT * FROM jobs WHERE job_id=?", (second,)).fetchone()
        self.assertEqual(muse_roles.job_view(db, first_row)["possible_duplicate_ids"], [second])
        self.assertEqual(muse_roles.job_view(db, second_row)["possible_duplicate_ids"], [first])
        db.close()

    def test_filters_required_experience_but_not_preferred_experience(self):
        self.preferences["search"]["max_required_years_experience"] = 2
        job = {"company": "Acme", "title": "Software Engineer I", "location": "Seattle",
               "work_arrangement": "on_site", "description": "Required: 5 years of experience."}
        included, _, blockers = muse_roles.evaluate(job, self.profile, self.preferences)
        self.assertFalse(included)
        self.assertIn("Requires 5 years", blockers[0])
        job["description"] = "3 years of experience preferred."
        included, _, blockers = muse_roles.evaluate(job, self.profile, self.preferences)
        self.assertTrue(included)
        self.assertFalse(blockers)

    def test_entry_level_does_not_imply_an_experience_ceiling(self):
        self.assertIsNone(self.preferences["search"]["max_required_years_experience"])
        job = {"company": "Acme", "title": "Junior Software Engineer", "location": "New York, NY",
               "work_arrangement": "on_site", "description": "Experience: 0–3 years."}
        included, _, blockers = muse_roles.evaluate(job, self.profile, self.preferences)
        self.assertTrue(included)
        self.assertTrue(any("0–3 years" in item and "no experience threshold is configured" in item for item in blockers))

    def test_unknown_geography_and_authorization_remain_visible_as_blockers(self):
        self.preferences["search"]["locations"] = ["Seattle"]
        job = {"company": "Acme", "title": "Software Engineer I", "location": "Unknown",
               "work_arrangement": "unknown", "description": "Must be authorized to work in the US."}
        included, _, blockers = muse_roles.evaluate(job, self.profile, self.preferences)
        self.assertTrue(included)
        self.assertTrue(any("geographic fit is unknown" in item for item in blockers))
        self.assertTrue(any("human review" in item for item in blockers))

    def test_graduation_window_rejects_only_an_explicit_out_of_window_cohort(self):
        self.preferences["search"]["graduation_window"] = {"start_year": 2025, "end_year": 2027}
        job = {"company": "Acme", "title": "Software Engineer I", "location": "Seattle",
               "work_arrangement": "on_site", "description": "Applications open to the Class of 2024."}
        included, _, blockers = muse_roles.evaluate(job, self.profile, self.preferences)
        self.assertFalse(included)
        self.assertIn("graduation cohort", blockers[0])
        job["description"] = "Applications open to the Class of 2026."
        included, evidence, _ = muse_roles.evaluate(job, self.profile, self.preferences)
        self.assertTrue(included)
        self.assertTrue(any("Graduation cohort overlaps" in item for item in evidence))

    def test_source_adapters_normalize_greenhouse_and_lever_payloads(self):
        greenhouse = {"jobs": [{"id": 12, "title": "Engineer I", "location": {"name": "Remote"},
                                 "absolute_url": "https://boards.greenhouse.io/acme/jobs/12", "content": ""}]}
        lever = [{"id": "abc", "text": "Engineer I", "categories": {"location": "Remote", "workplaceType": "remote"},
                  "hostedUrl": "https://jobs.lever.co/acme/abc", "descriptionPlain": ""}]
        with patch.object(muse_roles, "fetch_json", side_effect=[greenhouse, lever]):
            gh, gh_kind, _ = muse_roles.discover_source({"type": "greenhouse", "tenant": "acme"})
            lv, lv_kind, _ = muse_roles.discover_source({"type": "lever", "tenant": "acme"})
        self.assertEqual((gh_kind, lv_kind), ("greenhouse", "lever"))
        self.assertEqual(gh[0]["company"], "acme")
        self.assertEqual(lv[0]["workplace_type"], "remote")

    def test_ashby_adapter_normalizes_listed_public_jobs(self):
        payload = {"jobs": [
            {"id": "job-1", "title": "Software Engineer I", "location": "New York City",
             "workplaceType": "Hybrid", "isRemote": True, "isListed": True,
             "jobUrl": "https://jobs.ashbyhq.com/acme/job-1", "descriptionPlain": "Build software."},
            {"id": "job-2", "title": "Hidden role", "location": "Remote", "isListed": False,
             "jobUrl": "https://jobs.ashbyhq.com/acme/job-2", "descriptionPlain": ""},
        ]}
        with patch.object(muse_roles, "fetch_json", return_value=payload) as fetch:
            jobs, kind, source_url = muse_roles.discover_source(
                {"type": "ashby", "tenant": "acme", "company": "Acme"}
            )
        self.assertEqual(kind, "ashby")
        self.assertEqual(source_url, "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "job-1")
        self.assertEqual(jobs[0]["workplace_type"], "Hybrid")
        self.assertEqual(jobs[0]["description"], "Build software.")
        fetch.assert_called_once_with(source_url, "api.ashbyhq.com")

    def test_discovery_persists_source_results_and_obeys_shortlist_limit(self):
        self.preferences["search"]["enabled_sources"] = [
            {"type": "greenhouse", "tenant": "acme"},
            {"type": "lever", "tenant": "acme"},
        ]
        self.preferences["search"]["max_roles_to_review_per_run"] = 2
        self.paths["preferences"].write_text(json.dumps(self.preferences), encoding="utf-8")
        first = {"id": "gh-1", "company": "Acme", "title": "Software Engineer I", "location": "Seattle",
                 "url": "https://jobs.example/1", "description": ""}
        second = {**first, "id": "gh-2", "url": "https://jobs.example/2"}
        third = {**first, "id": "lev-3", "url": "https://jobs.example/3"}
        results = [([first, second], "greenhouse", "https://boards-api.greenhouse.io/feed"),
                   ([third], "lever", "https://api.lever.co/feed")]
        args = argparse.Namespace(command="discover", root=self.root)
        with patch.object(muse_roles, "discover_source", side_effect=results), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(muse_roles.run(args), 0)
        db = muse_roles.connect(self.paths["database"])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        runs = db.execute("SELECT result, detail FROM source_runs ORDER BY run_id").fetchall()
        db.close()
        self.assertEqual([row["result"] for row in runs], ["OK", "OK"])
        self.assertIn("scanned=1", runs[1]["detail"])

    def test_discovery_scans_past_irrelevant_feed_prefix_within_shortlist_limit(self):
        self.preferences["search"]["enabled_sources"] = [{"type": "greenhouse", "tenant": "acme"}]
        self.preferences["search"]["max_roles_to_review_per_run"] = 1
        self.paths["preferences"].write_text(json.dumps(self.preferences), encoding="utf-8")
        records = [
            {"id": f"other-{index}", "company": "Acme", "title": "Marketing Manager", "location": "Seattle",
             "url": f"https://jobs.example/other-{index}", "description": ""}
            for index in range(4)
        ]
        history_path = self.root / "history.csv"
        with history_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["Job Title", "Company Name", "Location", "Job URL", "Status"])
            writer.writerow(["Software Engineer I", "Acme", "Seattle", "https://jobs.example/swe-1", "APPLIED"])
        db = muse_roles.connect(self.paths["database"])
        muse_roles.import_history_csv(db, history_path, "Simplify")
        db.close()
        records.append({"id": "swe-1", "company": "Acme", "title": "Software Engineer I", "location": "Seattle",
                        "url": "https://jobs.example/swe-1", "description": ""})
        records.append({"id": "swe-2", "company": "Acme", "title": "Software Engineer I", "location": "Seattle",
                        "url": "https://jobs.example/swe-2", "description": ""})
        args = argparse.Namespace(command="discover", root=self.root)
        with patch.object(muse_roles, "discover_source", return_value=(records, "greenhouse", "https://boards-api.greenhouse.io/feed")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(muse_roles.run(args), 0)
        result = json.loads(output.getvalue())
        self.assertEqual((result[0]["received"], result[0]["scanned"], result[0]["eligible"],
                  result[0]["history_suppressed"], result[0]["saved"]), (6, 6, 1, 1, 1))
        db = muse_roles.connect(self.paths["database"])
        saved_urls = [row["canonical_url"] for row in db.execute("SELECT canonical_url FROM jobs")]
        self.assertEqual(saved_urls, ["https://jobs.example/swe-2"])
        db.close()

        repeated_records = [
            {"id": "swe-2", "company": "Acme", "title": "Software Engineer I", "location": "Seattle",
             "url": "https://jobs.example/swe-2", "description": ""},
            {"id": "swe-3", "company": "Acme", "title": "Software Engineer I", "location": "Seattle",
             "url": "https://jobs.example/swe-3", "description": ""},
        ]
        with patch.object(muse_roles, "discover_source", return_value=(repeated_records, "greenhouse", "https://boards-api.greenhouse.io/feed")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(muse_roles.run(args), 0)
        repeated_result = json.loads(output.getvalue())[0]
        self.assertEqual((repeated_result["already_tracked"], repeated_result["saved"]), (1, 1))
        db = muse_roles.connect(self.paths["database"])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        db.close()

    def test_discovery_reports_new_changed_seen_and_resolved_counts(self):
        self.preferences["search"]["enabled_sources"] = [{"type": "greenhouse", "tenant": "acme"}]
        self.paths["preferences"].write_text(json.dumps(self.preferences), encoding="utf-8")
        role = {"id": "gh-1", "company": "Acme", "title": "Software Engineer I", "location": "Seattle",
                "url": "https://jobs.example/role", "description": "Initial description."}
        args = argparse.Namespace(command="discover", root=self.root)

        def run_discovery(record):
            with patch.object(muse_roles, "discover_source", return_value=([record], "greenhouse", "https://boards-api.greenhouse.io/feed")), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(muse_roles.run(args), 0)
            return json.loads(output.getvalue())[0]

        self.assertEqual(run_discovery(role)["new"], 1)
        self.assertEqual(run_discovery(role)["previously_seen"], 1)
        changed_role = {**role, "description": "Updated requirements."}
        self.assertEqual(run_discovery(changed_role)["changed"], 1)

        history_path = self.root / "history.csv"
        with history_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["Job Title", "Company Name", "Location", "Job URL", "Status"])
            writer.writerow([role["title"], role["company"], role["location"], role["url"], "APPLIED"])
        db = muse_roles.connect(self.paths["database"])
        muse_roles.import_history_csv(db, history_path, "Simplify")
        db.close()
        resolved = run_discovery(changed_role)
        self.assertEqual((resolved["resolved"], resolved["history_suppressed"], resolved["saved"]), (1, 1, 0))

    def test_source_status_retains_last_success_and_role_freshness_after_failure(self):
        db = muse_roles.connect(self.paths["database"])
        role = {"id": "gh-1", "company": "Acme", "title": "Software Engineer I", "location": "Seattle",
                "url": "https://jobs.example/role", "description": ""}
        with patch.object(muse_roles, "now", side_effect=["2026-10-10T09:00:00+00:00", "2026-10-11T09:00:00+00:00"]):
            muse_roles.save_record(db, role, "greenhouse", "acme_board", self.profile, self.preferences)
            muse_roles.save_record(db, role, "greenhouse", "acme_board", self.profile, self.preferences)
        db.execute("INSERT INTO source_runs(source, started_at, result, detail) VALUES (?, ?, ?, ?)",
               ("greenhouse:acme_board", "2026-10-10T10:00:00+00:00", "OK", "scanned=25 saved=2"))
        db.execute("INSERT INTO source_runs(source, started_at, result, detail) VALUES (?, ?, ?, ?)",
               ("greenhouse:acme_board", "2026-10-11T10:00:00+00:00", "FAILED", "feed unavailable"))
        db.commit()
        status = muse_roles.source_status(db, 1)
        db.close()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["last_status"], "FAILED")
        self.assertEqual(status[0]["last_detail"], "feed unavailable")
        self.assertEqual(status[0]["last_success_detail"], "scanned=25 saved=2")
        self.assertEqual(status[0]["tracked_roles"], 1)
        self.assertEqual(status[0]["last_role_seen_at"], "2026-10-11T09:00:00+00:00")

    def test_discovery_keeps_a_source_failure_when_another_source_succeeds(self):
        self.preferences["search"]["enabled_sources"] = [
            {"type": "greenhouse", "tenant": "offline"},
            {"type": "lever", "tenant": "healthy"},
        ]
        self.paths["preferences"].write_text(json.dumps(self.preferences), encoding="utf-8")
        role = {"id": "lev-1", "company": "Acme", "title": "Software Engineer I", "location": "Seattle",
                "url": "https://jobs.example/1", "description": ""}
        responses = [ValueError("feed unavailable"), ([role], "lever", "https://api.lever.co/feed")]
        args = argparse.Namespace(command="discover", root=self.root)
        with patch.object(muse_roles, "discover_source", side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(muse_roles.run(args), 0)
        db = muse_roles.connect(self.paths["database"])
        runs = db.execute("SELECT source, result, detail FROM source_runs ORDER BY run_id").fetchall()
        db.close()
        self.assertEqual([row["result"] for row in runs], ["FAILED", "OK"])
        self.assertIn("feed unavailable", runs[0]["detail"])

    def test_cli_ingest_list_exact_decision_and_export(self):
        self.paths["resume"].write_bytes(b"synthetic resume")
        self.profile["confirmed_by_user_at"] = "2026-10-09"
        self.paths["profile"].write_text(json.dumps(self.profile), encoding="utf-8")
        raw = {"company": "=1+1", "title": "Software Engineer I", "location": "Seattle",
               "url": "https://jobs.example/role", "id": "req-1", "description": ""}
        command = [sys.executable, str(Path(muse_roles.__file__).resolve()), "--root", str(self.root)]
        result = subprocess.run(command + ["ingest"], input=json.dumps(raw), text=True,
                                capture_output=True, check=True)
        job_id = json.loads(result.stdout)["saved"][0]
        subprocess.run(command + ["decide", job_id, "approve"], text=True, capture_output=True, check=True)
        listing = subprocess.run(command + ["list"], text=True, capture_output=True)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(json.loads(listing.stdout)[0]["decision"], "APPROVED")
        db = muse_roles.connect(self.paths["database"])
        approval = db.execute("SELECT resume_sha256 FROM decisions WHERE status='APPROVED'").fetchone()
        db.close()
        self.assertEqual(approval["resume_sha256"], hashlib.sha256(b"synthetic resume").hexdigest())
        subprocess.run(command + ["decide", job_id, "revoke"], text=True, capture_output=True, check=True)
        export_path = self.paths["exports"] / "roles.csv"
        subprocess.run(command + ["export"], text=True, capture_output=True, check=True)
        with export_path.open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["decision"], "REVOKED")
        self.assertEqual(row["company"], "'=1+1")
        self.assertEqual(row["application_state"], "NOT_STARTED")

    def test_csv_history_import_is_idempotent_and_not_application_evidence(self):
        history_path = self.root / "simplify.csv"
        headers = ["Job Title", "Company Name", "Location", "Job URL", "Applied Date",
                   "Status", "Status Date", "Archived", "Date Archived", "Notes", "job_type"]
        records = [
            ["Software Engineer I", "Acme", "Seattle", "https://jobs.example/role?utm_source=feed",
             "2026-10-01", "APPLIED", "2026-10-01", "No", "", "", "Full-time"],
            ["Software Engineer I", "Acme", "Seattle", "https://jobs.example/role",
             "", "SAVED", "2026-10-02", "No", "", "", "Full-time"],
            ["Engineer I", "Other", "Seattle", "https://jobs.example/other",
             "", "SAVED", "2026-10-02", "No", "", "", "Full-time"],
        ]
        with history_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerows(records)

        first = self.cli(self.root, "history", "import", str(history_path), "--source", "Simplify")
        self.assertEqual(first.returncode, 0, first.stderr)
        summary = json.loads(first.stdout)
        self.assertEqual((summary["rows"], summary["unique_urls"], summary["duplicate_url_rows"]), (3, 2, 1))
        self.assertEqual(summary["status_conflicts"][0]["statuses"], ["APPLIED", "SAVED"])

        repeated = self.cli(self.root, "history", "import", str(history_path), "--source", "Simplify")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertTrue(json.loads(repeated.stdout)["already_imported"])

        db = muse_roles.connect(self.paths["database"])
        raw = {"company": "Acme", "title": "Software Engineer I", "location": "Seattle",
               "url": "https://jobs.example/role", "description": ""}
        job_id, included = muse_roles.save_record(db, raw, "muse", "observed", self.profile, self.preferences)
        self.assertTrue(included)
        job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        view = muse_roles.job_view(db, job)
        self.assertEqual(view["application_state"], "NOT_STARTED")
        self.assertEqual({item["status"] for item in view["imported_history"]}, {"APPLIED", "SAVED"})
        self.assertIn("conflicting statuses", muse_roles.history_requires_reconciliation(view["imported_history"]))
        preflight = muse_roles.preflight(db, self.root, job_id)
        self.assertTrue(any("conflicting statuses" in reason for reason in preflight["reasons"]))
        db.commit()
        db.close()

        default_list = self.cli(self.root, "list")
        self.assertEqual(default_list.returncode, 0, default_list.stderr)
        self.assertEqual(json.loads(default_list.stdout), [])
        full_list = self.cli(self.root, "list", "--include-history")
        self.assertEqual(full_list.returncode, 0, full_list.stderr)
        self.assertEqual(len(json.loads(full_list.stdout)), 1)

        exported = self.cli(self.root, "export")
        self.assertEqual(exported.returncode, 0, exported.stderr)
        with (self.paths["exports"] / "roles.csv").open(encoding="utf-8-sig", newline="") as stream:
            exported_rows = list(csv.DictReader(stream))
        self.assertEqual(len(exported_rows), 2)
        tracked = next(row for row in exported_rows if row["record_type"] == "TRACKED_ROLE")
        history_only = next(row for row in exported_rows if row["record_type"] == "IMPORTED_HISTORY")
        self.assertEqual(tracked["history_status"], "APPLIED | SAVED")
        self.assertEqual(tracked["application_state"], "NOT_STARTED")
        self.assertEqual(history_only["history_status"], "SAVED")
        self.assertEqual(history_only["application_state"], "HISTORY_ONLY")

    def test_new_history_snapshot_updates_matching_rows_and_retains_omitted_roles(self):
        headers = ["Job Title", "Company Name", "Location", "Job URL", "Status"]
        first_path = self.root / "history-first.csv"
        with first_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerow(["Engineer I", "Acme", "Seattle", "https://jobs.example/one", "APPLIED"])
            writer.writerow(["Engineer I", "Acme", "Seattle", "https://jobs.example/two", "SAVED"])
        db = muse_roles.connect(self.paths["database"])
        muse_roles.import_history_csv(db, first_path, "Simplify")

        next_path = self.root / "history-next.csv"
        with next_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerow(["Engineer I", "Acme", "Seattle", "https://jobs.example/one", "INTERVIEWING"])
        muse_roles.import_history_csv(db, next_path, "Simplify")

        updated = muse_roles.imported_history_for_url(db, "https://jobs.example/one")
        retained = muse_roles.imported_history_for_url(db, "https://jobs.example/two")
        self.assertEqual([row["status"] for row in updated], ["INTERVIEWING"])
        self.assertEqual([row["status"] for row in retained], ["SAVED"])
        db.close()

    def test_possible_history_match_is_visible_without_suppressing_role(self):
        history_path = self.root / "history.csv"
        with history_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["Job Title", "Company Name", "Location", "Job URL", "Status"])
            writer.writerow(["Software Engineer I", "Acme", "Seattle", "https://old.example/job", "APPLIED"])
        db = muse_roles.connect(self.paths["database"])
        muse_roles.import_history_csv(db, history_path, "Simplify")
        raw = {"company": "Acme", "title": "Software Engineer I", "location": "Seattle",
               "url": "https://new.example/job", "description": ""}
        job_id, included = muse_roles.save_record(db, raw, "ashby", "acme", self.profile, self.preferences)
        self.assertTrue(included)
        db.commit()
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        view = muse_roles.job_view(db, row)
        self.assertEqual(view["imported_history"], [])
        self.assertEqual(view["possible_history_matches"][0]["status"], "APPLIED")
        self.assertEqual(view["possible_history_matches"][0]["canonical_url"], "https://old.example/job")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        db.close()

        listing = self.cli(self.root, "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(len(json.loads(listing.stdout)), 1)

    def test_non_https_history_is_preserved_as_unmatchable_review_hint(self):
        history_path = self.root / "history.csv"
        with history_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["Job Title", "Company Name", "Location", "Job URL", "Status"])
            writer.writerow(["Software Engineer I", "Acme", "Seattle", "http://old.example/job", "APPLIED"])
            writer.writerow(["Software Engineer I", "Acme", "Seattle", "http://older.example/job", "APPLIED"])
        db = muse_roles.connect(self.paths["database"])
        summary = muse_roles.import_history_csv(db, history_path, "Simplify")
        self.assertEqual((summary["rows"], summary["unique_urls"], summary["duplicate_url_rows"]), (2, 0, 0))
        self.assertEqual(summary["unmatchable_url_rows"][0]["csv_line"], 2)

        raw = {"company": "Acme", "title": "Software Engineer I", "location": "Seattle",
               "url": "https://new.example/job", "description": ""}
        self.assertEqual(muse_roles.classify_discovery_record(db, raw, "ashby", "acme"), "previously_seen")
        job_id, included = muse_roles.save_record(db, raw, "ashby", "acme", self.profile, self.preferences)
        self.assertTrue(included)
        db.commit()
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        hint = muse_roles.job_view(db, row)["possible_history_matches"][0]
        self.assertEqual(hint["source_url"], "http://old.example/job")
        self.assertIn("credential-free HTTPS", hint["url_error"])
        self.assertEqual(hint["canonical_url"], "")
        self.assertEqual(muse_roles.imported_history_for_url(db, raw["url"]), [])

        next_path = self.root / "history-next.csv"
        with next_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["Job Title", "Company Name", "Location", "Job URL", "Status"])
            writer.writerow(["Software Engineer I", "Acme", "Seattle", "http://old.example/job", "INTERVIEWING"])
        muse_roles.import_history_csv(db, next_path, "Simplify")
        hints = muse_roles.possible_history_matches(
            db, "Acme", "Software Engineer I", "Seattle", raw["url"]
        )
        hint_statuses = {item["source_url"]: item["status"] for item in hints}
        db.close()
        self.assertEqual(hint_statuses, {
            "http://old.example/job": "INTERVIEWING",
            "http://older.example/job": "APPLIED",
        })

        exported = self.cli(self.root, "export")
        self.assertEqual(exported.returncode, 0, exported.stderr)
        with (self.paths["exports"] / "roles.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        history_row = next(row for row in rows if row["record_type"] == "IMPORTED_HISTORY")
        self.assertEqual(history_row["url"], "http://old.example/job")
        self.assertIn("credential-free HTTPS", history_row["notes"])
        self.assertEqual(history_row["application_state"], "HISTORY_ONLY")

    def test_approval_requires_the_single_resume_and_confirmed_profile(self):
        db = muse_roles.connect(self.paths["database"])
        job = {"company": "Acme", "title": "Software Engineer I", "location": "Seattle", "url": "https://jobs.example/role"}
        job_id, _ = muse_roles.save_record(db, job, "muse", "observed", self.profile, self.preferences)
        db.commit()
        db.close()
        command = [sys.executable, str(Path(muse_roles.__file__).resolve()), "--root", str(self.root), "decide", job_id, "approve"]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("completed private setup", result.stderr)

    def test_preflight_and_attempt_start_reject_unapproved_role(self):
        db = muse_roles.connect(self.paths["database"])
        raw = {"company": "Acme", "title": "Software Engineer I", "location": "Seattle",
               "url": "https://jobs.example/role", "id": "req-1", "description": ""}
        job_id, _ = muse_roles.save_record(db, raw, "muse", "observed", self.profile, self.preferences)
        db.commit()
        db.close()
        self.paths["resume"].write_bytes(b"synthetic resume")
        self.profile["confirmed_by_user_at"] = "2026-10-09"
        self.paths["profile"].write_text(json.dumps(self.profile), encoding="utf-8")
        blocked = self.cli(self.root, "preflight", job_id)
        self.assertEqual(json.loads(blocked.stdout)["status"], "BLOCKED")
        start = self.cli(self.root, "attempt", "start", job_id)
        self.assertEqual(start.returncode, 2)
        db = muse_roles.connect(self.paths["database"])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        db.close()

    def test_preflight_binds_destination_resume_policy_and_current_approval(self):
        job_id = self.approved_job(destination="https://apply.example/form?utm_source=board")
        ready = self.cli(self.root, "preflight", job_id)
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertEqual(json.loads(ready.stdout)["status"], "READY")
        wrong_destination = self.cli(self.root, "preflight", job_id, "--destination", "https://other.example/apply")
        self.assertEqual(json.loads(wrong_destination.stdout)["status"], "BLOCKED")
        self.paths["resume"].write_bytes(b"changed synthetic resume")
        changed_resume = self.cli(self.root, "preflight", job_id)
        self.assertIn("resume", ";".join(json.loads(changed_resume.stdout)["reasons"]))
        self.paths["resume"].write_bytes(b"synthetic resume")
        self.preferences["applications"]["narrative_answer_policy"] = "draft_from_approved_facts"
        self.paths["preferences"].write_text(json.dumps(self.preferences), encoding="utf-8")
        changed_policy = self.cli(self.root, "preflight", job_id)
        self.assertIn("policy changed", ";".join(json.loads(changed_policy.stdout)["reasons"]))
        self.preferences["applications"]["narrative_answer_policy"] = "review_each"
        self.paths["preferences"].write_text(json.dumps(self.preferences), encoding="utf-8")
        changed_profile = dict(self.profile, preferred_name="Updated synthetic name")
        self.paths["profile"].write_text(json.dumps(changed_profile), encoding="utf-8")
        changed_facts = self.cli(self.root, "preflight", job_id)
        self.assertIn("candidate facts", ";".join(json.loads(changed_facts.stdout)["reasons"]))
        self.paths["profile"].write_text(json.dumps(self.profile), encoding="utf-8")
        db = muse_roles.connect(self.paths["database"])
        db.execute("UPDATE jobs SET title='Senior Software Engineer' WHERE job_id=?", (job_id,))
        db.commit()
        db.close()
        changed_posting = self.cli(self.root, "preflight", job_id)
        self.assertIn("role details changed", ";".join(json.loads(changed_posting.stdout)["reasons"]))
        db = muse_roles.connect(self.paths["database"])
        db.execute("UPDATE jobs SET title='Software Engineer I' WHERE job_id=?", (job_id,))
        db.commit()
        db.close()
        self.cli(self.root, "decide", job_id, "revoke")
        revoked = self.cli(self.root, "preflight", job_id)
        self.assertIn("REVOKED", ";".join(json.loads(revoked.stdout)["reasons"]))

    def test_attempts_pause_for_review_and_require_resolution_before_resume(self):
        job_id = self.approved_job(description="Must be authorized to work in the US.")
        preflight_result = self.cli(self.root, "preflight", job_id)
        self.assertEqual(json.loads(preflight_result.stdout)["status"], "NEEDS_HUMAN")
        started = self.cli(self.root, "attempt", "start", job_id)
        self.assertEqual(started.returncode, 0, started.stderr)
        attempt = json.loads(started.stdout)
        self.assertEqual(attempt["state"], "NEEDS_HUMAN")
        no_resolution = self.cli(self.root, "attempt", "update", attempt["attempt_id"], "--state", "IN_PROGRESS")
        self.assertEqual(no_resolution.returncode, 2)
        self.assertIn("--resolution", no_resolution.stderr)
        resumed = self.cli(self.root, "attempt", "update", attempt["attempt_id"], "--state", "IN_PROGRESS",
                           "--resolution", "User confirmed eligibility based on the employer's stated requirement.")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.cli(self.root, "decide", job_id, "revoke")
        revoked_before_submit = self.cli(self.root, "attempt", "update", attempt["attempt_id"],
                                         "--state", "SUBMITTING", "--stage", "final review")
        self.assertEqual(revoked_before_submit.returncode, 2)
        self.assertIn("REVOKED", revoked_before_submit.stderr)

    def test_uncertain_submission_cannot_be_retried_and_keeps_event_history(self):
        job_id = self.approved_job()
        started = self.cli(self.root, "attempt", "start", job_id)
        attempt_id = json.loads(started.stdout)["attempt_id"]
        self.cli(self.root, "attempt", "update", attempt_id, "--state", "SUBMITTING", "--stage", "submit")
        unknown = self.cli(self.root, "attempt", "update", attempt_id, "--state", "SUBMISSION_UNKNOWN",
                           "--stage", "waiting for confirmation", "--evidence", "Submit action may have completed; confirmation unavailable.",
                           "--evidence-source", "agent_observed")
        self.assertEqual(unknown.returncode, 0, unknown.stderr)
        retry = self.cli(self.root, "attempt", "start", job_id)
        self.assertEqual(retry.returncode, 2)
        self.assertIn("SUBMISSION_UNKNOWN", retry.stderr)
        invalid_retry = self.cli(self.root, "attempt", "update", attempt_id, "--state", "IN_PROGRESS")
        self.assertEqual(invalid_retry.returncode, 2)
        detail = self.cli(self.root, "attempt", "show", attempt_id)
        self.assertEqual(len(json.loads(detail.stdout)["events"]), 3)

    def test_interrupted_in_progress_attempt_can_be_reconciled_as_unknown(self):
        job_id = self.approved_job()
        attempt_id = json.loads(self.cli(self.root, "attempt", "start", job_id).stdout)["attempt_id"]
        missing_evidence = self.cli(self.root, "attempt", "update", attempt_id,
                                    "--state", "SUBMISSION_UNKNOWN")
        self.assertEqual(missing_evidence.returncode, 2)
        unknown = self.cli(self.root, "attempt", "update", attempt_id,
                           "--state", "SUBMISSION_UNKNOWN", "--stage", "interrupted during browser session",
                           "--evidence", "Session ended before submission status could be determined.",
                           "--evidence-source", "agent_observed")
        self.assertEqual(unknown.returncode, 0, unknown.stderr)
        retry = self.cli(self.root, "attempt", "start", job_id)
        self.assertEqual(retry.returncode, 2)
        self.assertIn("SUBMISSION_UNKNOWN", retry.stderr)

    def test_submitting_attempt_cannot_be_closed_without_resolving_uncertainty(self):
        job_id = self.approved_job()
        attempt_id = json.loads(self.cli(self.root, "attempt", "start", job_id).stdout)["attempt_id"]
        self.cli(self.root, "attempt", "update", attempt_id, "--state", "SUBMITTING", "--stage", "submit")
        closed = self.cli(self.root, "attempt", "update", attempt_id, "--state", "CLOSED",
                          "--evidence", "No confirmation was observed.")
        self.assertEqual(closed.returncode, 2)
        self.assertIn("Invalid attempt transition", closed.stderr)

    def test_applied_requires_confirmation_evidence_and_updates_csv(self):
        job_id = self.approved_job()
        attempt_id = json.loads(self.cli(self.root, "attempt", "start", job_id).stdout)["attempt_id"]
        self.cli(self.root, "attempt", "update", attempt_id, "--state", "SUBMITTING", "--stage", "submit")
        missing_evidence = self.cli(self.root, "attempt", "update", attempt_id, "--state", "APPLIED")
        self.assertEqual(missing_evidence.returncode, 2)
        self.assertIn("confirmation evidence", missing_evidence.stderr)
        applied = self.cli(self.root, "attempt", "update", attempt_id, "--state", "APPLIED",
                           "--evidence", "Application confirmation page shows Acme and requisition req-1.",
                           "--evidence-source", "job_confirmation")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.cli(self.root, "export")
        with (self.paths["exports"] / "roles.csv").open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["application_state"], "APPLIED")
        self.assertTrue(row["applied_at"])

    def test_closed_attempt_needs_reason_for_new_attempt(self):
        job_id = self.approved_job()
        attempt_id = json.loads(self.cli(self.root, "attempt", "start", job_id).stdout)["attempt_id"]
        closed = self.cli(self.root, "attempt", "update", attempt_id, "--state", "CLOSED",
                          "--evidence", "User chose not to continue this application.")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        no_reason = self.cli(self.root, "attempt", "start", job_id)
        self.assertEqual(no_reason.returncode, 2)
        self.assertIn("--reason", no_reason.stderr)
        restarted = self.cli(self.root, "attempt", "start", job_id, "--reason", "User explicitly asked to resume.")
        self.assertEqual(restarted.returncode, 0, restarted.stderr)


if __name__ == "__main__":
    unittest.main()