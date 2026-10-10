import argparse
import contextlib
import csv
import hashlib
import io
import json
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

    def test_initialize_preserves_existing_private_configuration(self):
        profile_path = self.paths["profile"]
        profile_path.write_text('{"private":"kept"}', encoding="utf-8")
        muse_roles.initialize(self.root)
        self.assertEqual(json.loads(profile_path.read_text(encoding="utf-8")), {"private": "kept"})

    def test_cli_init_and_inspect(self):
        root = self.root / "fresh"
        command = [sys.executable, str(Path(muse_roles.__file__).resolve()), "--root", str(root)]
        initialized = subprocess.run(command + ["init"], text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(initialized.stdout)["state"].replace("\\", "/"), "private/state/roles.sqlite3")
        inspected = subprocess.run(command + ["inspect"], text=True, capture_output=True, check=True)
        summary = json.loads(inspected.stdout)
        self.assertIsNone(summary["resume_sha256"])
        self.assertTrue(summary["missing"])

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
        self.assertIn("considered=1", runs[1]["detail"])

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


if __name__ == "__main__":
    unittest.main()