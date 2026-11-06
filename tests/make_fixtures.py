#!/usr/bin/env python3
"""
Write the offline test data: a KoboToolbox asset (the deployed form, as the API returns it) and a set of
synthetic submissions in the shape of the API's data endpoint.  Nothing here is real survey data.

Usage
  python3 tests/make_fixtures.py <XLSForm .xlsx>        # writes tests/fixture_asset.json, tests/fixture_submissions.json

The asset is built from the XLSForm itself, so labels, list names and group paths match the deployed form.
The submissions follow the form's routing (consent, roles, the six scenario versions, the systems repeat) and
deliberately include names, telephone numbers and free text, so the tests can prove none of it is published.
"""
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

from openpyxl import load_workbook

HERE = os.path.dirname(os.path.abspath(__file__))
KABUL = timezone(timedelta(hours=4, minutes=30))
V_OLD, V_NEW = "vRev4fixtureAAAAAAAAAA", "vRev5fixtureBBBBBBBBBB"


def asset_from_xlsform(path):
    wb = load_workbook(path, read_only=True)
    langs = ["English (en)", "Dari (prs)", "Pashto (ps)"]
    survey, stack = [], []
    ws = wb["survey"]
    rows = list(ws.iter_rows(values_only=True))
    head = list(rows[0])
    for r in rows[1:]:
        d = dict(zip(head, r))
        t = (d.get("type") or "").strip()
        if not t:
            continue
        kind = t.replace(" ", "_")
        row = {"name": d.get("name"), "$kuid": f"k{len(survey):04d}"}
        if kind in ("begin_group", "begin_repeat"):
            stack.append(d["name"])
            row["type"] = kind
        elif kind in ("end_group", "end_repeat"):
            stack.pop()
            survey.append({"type": kind, "$kuid": f"k{len(survey):04d}"})
            continue
        else:
            parts = t.split()
            if parts[0] in ("select_one", "select_multiple") and len(parts) > 1:
                row["type"], row["select_from_list_name"] = parts[0], parts[1]
            else:
                row["type"] = parts[0]
        row["$xpath"] = "/".join(stack if kind.startswith("begin") else stack + [d["name"]])
        row["$autoname"] = d.get("name")
        # Row labels are left out (the dashboard reads only the choice labels), which keeps the fixture small.
        if d.get("calculation"):
            row["calculation"] = d["calculation"]
        survey.append(row)
    choices = []
    ws = wb["choices"]
    rows = list(ws.iter_rows(values_only=True))
    head = list(rows[0])
    for r in rows[1:]:
        d = dict(zip(head, r))
        if not d.get("list_name"):
            continue
        choices.append({"list_name": d["list_name"], "name": str(d["name"]), "label": [d.get(f"label::{lang}") for lang in langs]})
    return {
        "uid": "aFixtureAfgOhSurvey00000",
        "name": "Afghanistan OH data mapping survey v5 (fixture)",
        "asset_type": "survey",
        "has_deployment": True,
        "deployment__active": True,
        "deployed_version_id": V_NEW,
        "deployed_versions": {"count": 2, "results": [
            {"uid": V_NEW, "date_deployed": "2026-11-06T22:10:00.000000Z"},
            {"uid": V_OLD, "date_deployed": "2026-11-02T04:00:00.000000Z"},
        ]},
        "content": {
            "schema": "1",
            "survey": survey,
            "choices": choices,
            "settings": {"id_string": "afg_oh_mapping_survey", "default_language": "English (en)", "style": "pages"},
            "translated": ["label", "hint", "constraint_message"],
            "translations": langs,
        },
    }


def pick(rng, weights):
    keys, w = zip(*weights.items())
    return rng.choices(keys, weights=w, k=1)[0]


SYS_BY_SECTOR = {
    "human": {"ndsr_ibs": 6, "ndsr_ebs": 4, "dhis2": 5, "eios": 1, "herams": 1, "polio": 2, "prog_other": 2, "spreadsheet": 2, "paper": 2, "msg_group": 2, "other": 1},
    "poe": {"poe": 6, "ndsr_ebs": 2, "paper": 3, "spreadsheet": 1, "msg_group": 1},
    "lab": {"lab_ph": 5, "lab_vet": 3, "amr": 2, "spreadsheet": 2, "paper": 1, "other": 1},
    "animal": {"ved": 5, "vfu": 5, "vfu_digital": 2, "vacc": 3, "lab_vet": 2, "vet_clinic": 1, "slaughter": 1, "lvst_pop": 1, "paper": 2, "msg_group": 1},
    "food": {"food": 4, "slaughter": 2, "paper": 2},
    "wildlife": {"wildlife": 3, "vector": 1, "spreadsheet": 1},
    "env": {"met": 2, "wash": 2, "fews": 1, "spreadsheet": 1},
    "wash": {"wash": 4, "spreadsheet": 1},
    "emergency": {"disaster": 4, "eoc": 2, "msg_group": 1},
    "data": {"dhis2": 3, "pop": 2, "spreadsheet": 2},
    "other": {"spreadsheet": 2, "paper": 1, "other": 1},
}
PROV_W = {"AF01": 14, "AF32": 6, "AF06": 6, "AF27": 5, "AF21": 5, "AF19": 3, "AF09": 2, "AF17": 2, "AF10": 2, "AF11": 2,
          "AF30": 2, "AF14": 2, "AF15": 1, "AF07": 1, "AF05": 1, "AF03": 2, "AF02": 1, "AF13": 1, "AF29": 1, "AF28": 1,
          "AF18": 1, "AF33": 1}


def submissions(n=96, seed=2026):
    rng = random.Random(seed)
    out = []
    day_weights = {"2026-11-03": 7, "2026-11-04": 15, "2026-11-05": 22, "2026-11-06": 19, "2026-11-07": 12}
    sid = 101
    # two test submissions before launch, to be marked "Not approved"
    plan = [("2026-11-02", "test")] * 2 + [(pick(rng, day_weights), "real") for _ in range(n)]
    plan.sort()
    for day, kind in plan:
        hour = rng.randint(8, 18) if kind == "real" else 15
        minute = rng.randint(0, 59)
        local = datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=KABUL)
        rec = {"_id": sid, "formhub/uuid": "fixture", "meta/instanceID": f"uuid:fixture-{sid}",
               "_validation_status": {}, "__version__": V_OLD if local < datetime(2026, 11, 7, 2, 40, tzinfo=KABUL) or rng.random() < 0.12 else V_NEW}
        sid += 1
        if kind == "test":
            rec["_validation_status"] = {"uid": "validation_status_not_approved", "label": "Not Approved"}
        consent = "no" if (kind == "real" and rng.random() < 0.035) else "yes"
        rec["consent"] = consent
        dur = 0
        if consent == "yes":
            function = pick(rng, {"run": 28, "report": 28, "direct": 12, "enter": 10, "community": 13, "other": 5})
            if function == "community":
                ministry, sector = "moph", pick(rng, {"human": 8, "animal": 2})
                workplace, employer = "community", pick(rng, {"volunteer": 6, "ministry": 2, "nngo": 2})
                level = "field"
                mode = pick(rng, {"in_person": 7, "phone": 3})
            else:
                ministry = pick(rng, {"moph": 44, "mail": 30, "nepa": 5, "andma": 4, "none": 12, "other": 3, "mrrd": 2})
                sector = {
                    "moph": pick(rng, {"human": 68, "lab": 14, "poe": 10, "data": 8}),
                    "mail": pick(rng, {"animal": 72, "lab": 16, "food": 6, "wildlife": 6}),
                    "nepa": pick(rng, {"env": 3, "wildlife": 2, "wash": 1}),
                    "andma": "emergency", "mrrd": "wash", "other": "other",
                    "none": pick(rng, {"human": 4, "animal": 3, "data": 2, "lab": 1}),
                }[ministry]
                employer = "ministry" if ministry not in ("none",) else pick(rng, {"un": 6, "ingo": 3, "nngo": 2, "private": 1})
                workplace = pick(rng, {"ministry_central": 3, "ministry_prov": 5, "facility": 2, "lab": 1, "poe": 1}) if employer == "ministry" else ("un" if employer == "un" else "ngo" if employer in ("ingo", "nngo") else "private")
                if sector == "lab":
                    workplace = "lab"
                if sector == "poe":
                    workplace = "poe"
                level = pick(rng, {"national": 30, "provincial": 45, "district": 15, "field": 7, "several": 3})
                mode = pick(rng, {"online": 66, "phone": 20, "in_person": 14})
            rec["mode"] = mode
            rec["sec_b/workplace"] = workplace
            rec["sec_b/employer"] = employer
            if employer == "ministry" or workplace in ("ministry_central", "ministry_prov"):
                rec["sec_b/ministry"] = ministry
            sectors = [sector] + ([pick(rng, {"data": 2, "emergency": 1, "lab": 1})] if rng.random() < 0.18 else [])
            sectors = list(dict.fromkeys(sectors))
            rec["sec_b/sectors"] = " ".join(sectors)
            if len(sectors) > 1:
                rec["sec_b/sector_main"] = sector
            rec["sec_b/level"] = level
            if level != "national":
                provs = {pick(rng, PROV_W)}
                if rng.random() < 0.15:
                    provs.add(pick(rng, PROV_W))
                rec["sec_b/provinces"] = " ".join(sorted(provs))
            rec["sec_b/function"] = function
            rec["sec_b/gender"] = pick(rng, {"man": 77, "woman": 21, "prefer_not": 2})
            main_path = 1 if function != "community" else 0
            f0 = 1 if main_path and function in ("report", "run", "direct", "other") and sector != "data" else 0
            if sector == "lab":
                scen = "lab_mail" if ministry == "mail" else "lab_other"
            elif sector == "poe":
                scen = "poe"
            elif sector in ("animal", "food"):
                scen = "animal"
            elif sector in ("wildlife", "env", "wash"):
                scen = "env"
            else:
                scen = "human"
            rec.update({"main_sector": sector, "main_path": str(main_path), "f0_path": str(f0), "scen": scen,
                        "ask_ah": "0", "ask_ph": "0", "det_side": "h" if sector in ("human", "poe") else "a" if sector in ("animal", "wildlife") else "",
                        "cap": "3" if function == "run" else "2"})
            if function == "community":
                rec["sec_j/com_report"] = "deaths_animals unusual_illness"
                rec["sec_j/com_comments"] = "Free text that must never be published."
                dur = rng.randint(8, 16)
            else:
                n_sys = pick(rng, {"0": 10, "1": 25, "2": 30, "3": 20, "4": 15})
                rec["sec_d/n_systems"] = n_sys
                rec["sec_d/n_sys"] = n_sys
                cap = 3 if function == "run" else 2
                n_rep = min(int(n_sys), cap)
                rec["sec_d/n_rep"] = str(n_rep)
                if n_rep:
                    names = []
                    while len(names) < n_rep:
                        c = pick(rng, SYS_BY_SECTOR[sector])
                        if c not in names or c == "other":
                            names.append(c)
                    rec["sec_d/systems"] = []
                    for i, c in enumerate(names, 1):
                        item = {"sec_d/systems/pos": str(i), "sec_d/systems/sys_name": c,
                                "sec_d/systems/sys_role": "use", "sec_d/systems/sys_status": "active"}
                        if c == "other":
                            item["sec_d/systems/sys_name_other"] = "Provincial line list kept by Dr Example"
                        item["sec_d/systems/sys_display"] = item.get("sec_d/systems/sys_name_other", c)
                        rec["sec_d/systems"].append(item)
                    rec["sec_d/share_docs"] = pick(rng, {"upload_now": 15, "later": 35, "no": 50}) if mode == "online" else pick(rng, {"later": 40, "no": 60})
                if f0:
                    rec["sec_f/event_12m"] = pick(rng, {"yes": 4, "no": 5, "dk": 1})
                ok = pick(rng, {"yes": 55, "no": 45})
                rec["sec_i/interview_ok"] = ok
                if ok == "yes":
                    rec["sec_i/contact/contact_name"] = "Fixture Person %d" % rec["_id"]
                    rec["sec_i/contact/contact_phone"] = "+93 70 000 %04d" % rec["_id"]
                rec["sec_i/comments"] = "General comment free text, never to be published."
                dur = rng.randint(18, 58) if rng.random() > 0.04 else rng.randint(5, 9)
        else:
            dur = 1
        start = local - timedelta(minutes=dur)
        rec["start"] = start.isoformat(timespec="milliseconds")
        rec["end"] = local.isoformat(timespec="milliseconds")
        rec["_submission_time"] = (local + timedelta(minutes=rng.randint(0, 3))).astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        out.append(rec)
    # one community health worker who got past the online check by choosing another role
    for rec in out:
        if rec.get("sec_b/function") == "report" and rec.get("mode") == "online":
            rec["sec_b/workplace"] = "community"
            break
    return out


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asset = asset_from_xlsform(sys.argv[1])
    subs = submissions()
    with open(os.path.join(HERE, "fixture_asset.json"), "w", encoding="utf-8") as fh:
        json.dump(asset, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "fixture_submissions.json"), "w", encoding="utf-8") as fh:
        json.dump({"count": len(subs), "next": None, "previous": None, "results": subs}, fh, ensure_ascii=False, indent=1)
    print(f"wrote fixture_asset.json ({len(asset['content']['survey'])} rows) and fixture_submissions.json ({len(subs)} submissions)")


if __name__ == "__main__":
    main()
