"""Offline tests for scripts/update.py: python3 -m unittest discover -s tests"""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import update  # noqa: E402

NOW = datetime(2026, 11, 7, 14, 30, tzinfo=timezone.utc)


def load(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return json.load(fh)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
            cls.cfg = json.load(fh)
        cls.asset = load("fixture_asset.json")
        cls.subs = load("fixture_submissions.json")["results"]
        cls.form = update.read_form(cls.asset, cls.cfg)
        cls.status, cls.audit = update.build_status(cls.cfg, cls.form, cls.subs, NOW)


class TestForm(Base):
    def test_paths_come_from_the_deployed_form(self):
        p = self.form["paths"]
        self.assertEqual(p["function"], "sec_b/function")
        self.assertEqual(p["systems"], "sec_d/systems")
        self.assertEqual(p["interview_ok"], "sec_i/interview_ok")
        self.assertEqual(p["consent"], "consent")

    def test_paths_without_xpath_follow_the_groups(self):
        asset = copy.deepcopy(self.asset)
        for row in asset["content"]["survey"]:
            row.pop("$xpath", None)
        form = update.read_form(asset, self.cfg)
        self.assertEqual(form["paths"], self.form["paths"])

    def test_english_labels(self):
        L = self.form["labels"]
        self.assertEqual(L["ministry"]["mail"], "Ministry of Agriculture, Irrigation and Livestock (MAIL)")
        self.assertEqual(L["provinces"]["AF01"], "Kabul")
        self.assertEqual(len(L["sys_name"]), 35)

    def test_only_counted_fields_are_requested(self):
        wanted = sorted({v for k, v in self.form["paths"].items() if k != "sys_name"})
        for w in wanted:
            self.assertNotRegex(w, r"contact|comment|name_other|email|phone")


class TestCounts(Base):
    def test_totals(self):
        s = self.status["submissions"]
        self.assertEqual(s["received"], 98)
        self.assertEqual(s["not_approved"], 2)
        self.assertEqual(s["counted"], 96)
        self.assertEqual(s["respondents"] + s["declined"] + s["consent_missing"], s["counted"])
        self.assertEqual(sum(d["n"] for d in self.status["by_day"]), s["respondents"])

    def test_dimensions_add_up(self):
        R = self.status["submissions"]["respondents"]
        for d in ("mode", "level", "function", "gender", "main_sector"):
            self.assertEqual(sum(self.status["dims"][d].values()), R, d)
        self.assertEqual(sum(self.status["paths"].values()), R)

    def test_scenarios_only_for_the_scenario_path(self):
        f0 = sum(1 for s in self.subs if s.get("f0_path") == "1" and s.get("consent") == "yes"
                 and not (s.get("_validation_status") or {}).get("uid"))
        self.assertEqual(sum(self.status["scenarios"].values()), f0)

    def test_systems_codes_only(self):
        S = self.status["systems"]
        self.assertEqual(sum(S["named"].values()), S["described"])
        self.assertGreater(S["other"], 0)

    def test_flags(self):
        q = self.status["quality"]
        self.assertEqual(q["community_workplace_other_role"], 1)
        self.assertGreaterEqual(q["quick"], 1)
        self.assertEqual(sum(v["n"] for v in q["versions"]), self.status["submissions"]["counted"])
        self.assertTrue(any(v["current"] for v in q["versions"]))

    def test_count_from_excludes_early_returns(self):
        cfg = dict(self.cfg, count_from="2026-11-05T00:00:00+04:30")
        status, _ = update.build_status(cfg, self.form, self.subs, NOW)
        self.assertGreater(status["submissions"]["before_start"], 0)
        self.assertTrue(all(d["day"] >= "2026-11-05" for d in status["by_day"]))


class TestNothingPersonal(Base):
    def test_no_names_phones_or_free_text(self):
        text = json.dumps(self.status, ensure_ascii=False)
        for s in self.subs:
            for k, v in s.items():
                if isinstance(v, str) and any(x in k for x in ("contact", "comment", "name_other", "com_report")):
                    self.assertNotIn(v, text, k)
            for item in s.get("sec_d/systems") or []:
                other = item.get("sec_d/systems/sys_name_other")
                if other:
                    self.assertNotIn(other, text)
        self.assertNotIn("+93", text)


class TestEncryption(unittest.TestCase):
    def test_round_trip_and_wrong_password(self):
        from cryptography.exceptions import InvalidTag
        blob = update.encrypt(b'{"a": 1}', "a long team password")
        self.assertEqual(blob[:6], b"AFGOH1")
        self.assertEqual(update.decrypt(blob, "a long team password"), b'{"a": 1}')
        with self.assertRaises(InvalidTag):
            update.decrypt(blob, "the wrong password")

    def test_deterministic_and_data_dependent(self):
        a = update.encrypt(b"same", "pw-pw-pw-pw")
        self.assertEqual(a, update.encrypt(b"same", "pw-pw-pw-pw"))
        self.assertNotEqual(a[6:34], update.encrypt(b"other", "pw-pw-pw-pw")[6:34])


class TestFindProject(unittest.TestCase):
    cfg = {"kobo_server": "https://kf.example.org", "form_id": "afg_oh_mapping_survey", "asset_uid": None}

    def fake(self, assets):
        def get(url):
            if "/api/v2/assets/?" in url:
                return {"results": [{"uid": a["uid"], "has_deployment": a.get("deployed", True)} for a in assets], "next": None}
            uid = url.split("/api/v2/assets/")[1].split("/")[0]
            a = next(x for x in assets if x["uid"] == uid)
            return {"content": {"settings": {"id_string": a["form"]}}}
        return get

    def test_finds_the_one_matching_project(self):
        os.environ.pop("KOBO_ASSET_UID", None)
        get = self.fake([{"uid": "aOther", "form": "collar_obs_v1"}, {"uid": "aSurvey", "form": "afg_oh_mapping_survey"}])
        self.assertEqual(update.find_asset_uid(self.cfg, "token", get), "aSurvey")

    def test_prefers_the_deployed_copy(self):
        os.environ.pop("KOBO_ASSET_UID", None)
        get = self.fake([{"uid": "aDraft", "form": "afg_oh_mapping_survey", "deployed": False},
                         {"uid": "aLive", "form": "afg_oh_mapping_survey"}])
        self.assertEqual(update.find_asset_uid(self.cfg, "token", get), "aLive")

    def test_stops_when_nothing_matches(self):
        os.environ.pop("KOBO_ASSET_UID", None)
        # The error line is swallowed here: in GitHub Actions it would show as a failure annotation on the run.
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            update.find_asset_uid(self.cfg, "token", self.fake([{"uid": "aOther", "form": "x"}]))

    def test_variable_overrides_the_search(self):
        os.environ["KOBO_ASSET_UID"] = "aFromVariable"
        try:
            self.assertEqual(update.find_asset_uid(self.cfg, "token", lambda url: self.fail("no search expected")), "aFromVariable")
        finally:
            os.environ.pop("KOBO_ASSET_UID", None)


class TestCommandLine(unittest.TestCase):
    def test_fixture_run_writes_an_encrypted_file(self):
        out = os.path.join(HERE, "_out_status.json.enc")
        env = dict(os.environ)
        env.pop("GITHUB_ACTIONS", None)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "update.py"), "--fixture",
                            os.path.join(HERE, "fixture_submissions.json"), "--asset", os.path.join(HERE, "fixture_asset.json"),
                            "--password", "test password 123", "--out", out, "--now", "2026-11-07T14:30:00Z"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(out, "rb") as fh:
            blob = fh.read()
        os.remove(out)
        self.assertEqual(blob[:6], b"AFGOH1")
        data = json.loads(update.decrypt(blob, "test password 123"))
        self.assertEqual(data["submissions"]["respondents"], 91)
        self.assertNotIn("Fixture Person", r.stdout)

    def test_refuses_to_run_without_a_password(self):
        env = dict(os.environ)
        env.pop("DASHBOARD_PASSWORD", None)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "update.py"), "--fixture",
                            os.path.join(HERE, "fixture_submissions.json")], capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DASHBOARD_PASSWORD", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
