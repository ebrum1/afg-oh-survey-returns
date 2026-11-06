#!/usr/bin/env python3
"""
Build the encrypted data file for the Afghanistan One Health survey returns dashboard.

What it does
  1. Finds the survey on KoboToolbox by its form ID (afg_oh_mapping_survey in config.json), unless the
     repository variable KOBO_ASSET_UID or asset_uid in config.json names the project.
  2. Reads the deployed form, to learn where each field sits and what each answer code is called.
  3. Reads ONLY the fields it counts: submission time, start and end time, form version, validation status,
     consent, mode, workplace, employer, ministry, sectors, level, provinces, role, gender, the routing results
     (main path, scenario), the number of systems, the systems named, the documents offer and the interview
     opt-in.  It never requests names, telephone numbers, email addresses, comments or any other answer.  The
     systems repeat arrives whole; the script keeps only the system codes and drops the rest in memory.
  4. Writes counts only, encrypted with the dashboard password (AES-256-GCM with a key from PBKDF2-SHA256,
     200 000 rounds, as on the collar study map), to site/data/status.json.enc.  Nothing readable is published.

Usage
  KOBO_TOKEN=... DASHBOARD_PASSWORD=... python3 scripts/update.py          # live, as the workflow runs it
  KOBO_TOKEN=... DASHBOARD_PASSWORD=... python3 scripts/update.py --list   # also one line per submission
  python3 scripts/update.py --fixture tests/fixture_submissions.json --asset tests/fixture_asset.json --password test
  python3 scripts/update.py ... --plain site/data/status.json               # local testing only: also write it unencrypted

Standard library only, apart from `cryptography` for the encryption.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KABUL = timezone(timedelta(hours=4, minutes=30))          # Afghanistan, no daylight saving
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
MAGIC = b"AFGOH1"
PBKDF2_ROUNDS = 200_000
SCHEMA = 1

# The dimensions the page shows, with the field that holds each one.
DIMENSIONS = ["mode", "workplace", "employer", "ministry", "main_sector", "level", "function", "gender"]


# --------------------------------------------------------------------------- logging

def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"::warning::{msg}" if IN_ACTIONS else f"WARNING: {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"::error::{msg}" if IN_ACTIONS else f"ERROR: {msg}", flush=True)
    sys.exit(1)


class KoboHTTPError(Exception):
    def __init__(self, code: int, url: str):
        super().__init__(f"HTTP {code} for {url}")
        self.code = code


# --------------------------------------------------------------------------- HTTP

class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the API key to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_SameHostRedirect)


def http_get_json(url: str, token: str, tries: int = 4):
    """GET with the Kobo API key; retries network faults and 5xx/429 with back-off."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Token {token}",
        "Accept": "application/json",
        "User-Agent": "afg-oh-survey-returns-dashboard",
    })
    for attempt in range(1, tries + 1):
        try:
            with _OPENER.open(req, timeout=90) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if not (e.code >= 500 or e.code == 429) or attempt == tries:
                raise KoboHTTPError(e.code, url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries:
                raise
        wait = 10 * attempt
        log(f"  Kobo did not answer cleanly (attempt {attempt} of {tries}); retrying in {wait} s")
        time.sleep(wait)
    raise RuntimeError("unreachable")


def explain_http_error(err: KoboHTTPError, uid: str | None) -> str:
    if err.code in (401, 403):
        return (f"KoboToolbox refused the API key (HTTP {err.code}).  Check that the KOBO_TOKEN repository secret "
                "holds a current key.")
    if err.code == 404:
        return (f"KoboToolbox answered 'not found' (HTTP 404) for project {uid or '(none)'}.  Either the project ID "
                "is wrong, or the account behind KOBO_TOKEN cannot view this project's submissions.  Kobo answers "
                "404, not 403, when an account lacks access.")
    return f"KoboToolbox returned HTTP {err.code}."


# --------------------------------------------------------------------------- time

def parse_time(value) -> datetime | None:
    """Kobo's _submission_time is UTC without a zone; start and end carry the device's offset."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:?\d{2})?", s)
        if not m:
            return None
        zone = m.group(3) or ""
        if zone and ":" not in zone:
            zone = zone[:3] + ":" + zone[3:]
        try:
            t = datetime.fromisoformat(m.group(1) + zone)
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def kabul_day(t: datetime) -> str:
    return t.astimezone(KABUL).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- the deployed form

def read_form(asset: dict, cfg: dict) -> dict:
    """Paths, answer labels (English) and version dates from the asset as the Kobo API returns it."""
    content = asset.get("content") or {}
    survey = content.get("survey") or []
    translations = content.get("translations") or [None]
    lang = 0
    for i, t in enumerate(translations):
        if t and str(t).lower().startswith("english"):
            lang = i
            break

    # Where each field sits: Kobo gives $xpath; fall back to the group nesting.
    paths, lists, stack = {}, {}, []
    for row in survey:
        kind = str(row.get("type") or "")
        name = row.get("name") or row.get("$autoname")
        if kind in ("begin_group", "begin_repeat", "begin group", "begin repeat"):
            stack.append(name)
            paths.setdefault(name, row.get("$xpath") or "/".join(stack))
            continue
        if kind in ("end_group", "end_repeat", "end group", "end repeat"):
            if stack:
                stack.pop()
            continue
        if not name:
            continue
        paths.setdefault(name, row.get("$xpath") or "/".join(stack + [name]))
        if row.get("select_from_list_name"):
            lists[name] = row["select_from_list_name"]

    def label_of(ch: dict) -> str:
        lab = ch.get("label")
        if isinstance(lab, list):
            lab = lab[lang] if lang < len(lab) else (lab[0] if lab else "")
        return str(lab or ch.get("name") or "").strip()

    choice_labels: dict[str, dict] = defaultdict(dict)
    for ch in content.get("choices") or []:
        choice_labels[ch.get("list_name")][str(ch.get("name"))] = label_of(ch)

    f = cfg["fields"]
    resolved, missing = {}, []
    for role, name in f.items():
        if name in paths:
            resolved[role] = paths[name]
        else:
            missing.append(name)
    if missing:
        warn("These fields are not in the deployed form, so their figures will be empty: " + ", ".join(missing))

    labels = {}
    for role, name in f.items():
        lst = lists.get(name)
        if lst:
            labels[role] = dict(choice_labels.get(lst, {}))
    labels["main_sector"] = labels.get("sectors") or labels.get("sector_main") or labels.get("main_sector") or {}
    labels["provinces"] = labels.get("provinces", {})
    labels["sys_name"] = labels.get("sys_name", {})
    labels["scen"] = dict(cfg.get("scenario_labels") or {})

    versions = []
    dv = asset.get("deployed_versions") or {}
    for v in (dv.get("results") if isinstance(dv, dict) else dv) or []:
        versions.append({"uid": v.get("uid"), "deployed": iso(parse_time(v.get("date_deployed")))})
    return {
        "uid": asset.get("uid"),
        "name": asset.get("name") or "",
        "paths": resolved,
        "labels": labels,
        "versions": versions,
        "current_version": asset.get("deployed_version_id") or (versions[0]["uid"] if versions else None),
        "language": translations[lang] if translations and translations[lang] else "default",
    }


# --------------------------------------------------------------------------- Kobo reads

def find_asset_uid(cfg: dict, token: str, get=None) -> str:
    """The project ID: KOBO_ASSET_UID, else config asset_uid, else the one survey whose form ID matches."""
    get = get or (lambda url: http_get_json(url, token))
    uid = (os.environ.get("KOBO_ASSET_UID") or cfg.get("asset_uid") or "").strip()
    if uid:
        return uid
    base = cfg["kobo_server"].rstrip("/")
    url = f"{base}/api/v2/assets/?" + urllib.parse.urlencode({"q": "asset_type:survey", "limit": "200", "format": "json"})
    found, scanned = [], 0
    while url:
        page = get(url)
        for a in page.get("results", []):
            scanned += 1
            settings = (a.get("content") or {}).get("settings")
            if settings is None:                       # the list view may leave the form out; read the project
                settings = ((get(f"{base}/api/v2/assets/{a['uid']}/?format=json").get("content") or {}).get("settings") or {})
            if settings.get("id_string") == cfg["form_id"]:
                found.append(a)
        url = page.get("next")
    # Only the matching project is named in the log: the log of a public repository is public.
    if len(found) == 1:
        log(f"Project found by form ID {cfg['form_id']}: uid {found[0]['uid']} (checked {scanned} projects)")
        return found[0]["uid"]
    if not found:
        fail(f"No project with form ID {cfg['form_id']} is visible to the account behind KOBO_TOKEN "
             f"(checked {scanned} projects).  Share the project with that account, or set the repository variable "
             "KOBO_ASSET_UID to the project's ID (the part of its Kobo address after /forms/).")
    deployed = [a for a in found if a.get("has_deployment") or a.get("deployment__active")]
    if len(deployed) == 1:
        log(f"Project found by form ID {cfg['form_id']}: uid {deployed[0]['uid']} (the only deployed one of {len(found)})")
        return deployed[0]["uid"]
    fail(f"{len(found)} projects have form ID {cfg['form_id']}: {', '.join(a['uid'] for a in found)}.  "
         "Set the repository variable KOBO_ASSET_UID to the right one.")
    return ""


def fetch_submissions(cfg: dict, token: str, uid: str, form: dict) -> list[dict]:
    base = cfg["kobo_server"].rstrip("/")
    meta = ["_id", "_submission_time", "_validation_status", "__version__"]
    p = form["paths"]
    # The systems repeat is requested as a whole (Kobo cannot pick one field inside a repeat);
    # sys_name is read from it and everything else in it is dropped in build_status.
    wanted = meta + sorted({v for k, v in p.items() if k != "sys_name"})
    params = {"format": "json", "limit": "1000", "sort": json.dumps({"_id": 1}), "fields": json.dumps(wanted)}
    url = f"{base}/api/v2/assets/{uid}/data/?" + urllib.parse.urlencode(params)
    out, pages, reported = [], 0, None
    while url and pages < 200:
        page = http_get_json(url, token)
        reported = page.get("count", reported)
        out.extend(page.get("results", []))
        url = page.get("next")
        pages += 1
    if reported is not None and reported != len(out):
        warn(f"Kobo reported {reported} submissions but {len(out)} were read.")
    return out


# --------------------------------------------------------------------------- counting

def build_status(cfg: dict, form: dict, subs: list[dict], now: datetime) -> tuple[dict, list[dict]]:
    P = form["paths"]
    excluded_status = set(cfg.get("exclude_validation_status") or [])
    count_from = parse_time(cfg.get("count_from")) if cfg.get("count_from") else None
    quick = float(cfg.get("quick_minutes") or 10)

    def val(s: dict, role: str) -> str:
        path = P.get(role)
        v = s.get(path) if path else None
        if v is None and path:                          # tolerate a renamed group: match on the field name
            leaf = path.split("/")[-1]
            for k, x in s.items():
                if k.split("/")[-1] == leaf and not isinstance(x, list):
                    v = x
                    break
        return str(v).strip() if v is not None else ""

    def multi(s: dict, role: str) -> list[str]:
        return [x for x in val(s, role).split() if x]

    tally = Counter(received=len(subs))
    dims = {d: Counter() for d in DIMENSIONS}
    sectors_any, provinces, scen, paths = Counter(), Counter(), Counter(), Counter()
    prov_sector: dict[str, Counter] = defaultdict(Counter)
    ministry_gender: dict[str, Counter] = defaultdict(Counter)
    by_day, declined_by_day = Counter(), Counter()
    n_systems, sys_named, share_docs, interview = Counter(), Counter(), Counter(), Counter()
    sys_respondents = sys_instances = sys_other = 0
    durations = {"main": [], "community": []}
    quick_n = no_duration = chw_flag = 0
    versions = Counter()
    times: list[datetime] = []
    audit = []

    for s in subs:
        vstatus = (s.get("_validation_status") or {}).get("uid") or ""
        t = parse_time(s.get("_submission_time"))
        row = {"id": s.get("_id"), "time": t, "version": s.get("__version__", ""),
               "validation": vstatus.replace("validation_status_", "")}
        audit.append(row)
        if vstatus in excluded_status:
            tally["not_approved"] += 1
            row["outcome"] = "excluded: not approved"
            continue
        if t is None:
            tally["no_time"] += 1
            row["outcome"] = "excluded: no submission time"
            continue
        if count_from and t < count_from:
            tally["before_start"] += 1
            row["outcome"] = "excluded: before count_from"
            continue
        tally["counted"] += 1
        versions[s.get("__version__") or ""] += 1
        consent = val(s, "consent")
        if consent != "yes":
            tally["declined" if consent == "no" else "consent_missing"] += 1
            declined_by_day[kabul_day(t)] += 1
            row["outcome"] = "counted: declined consent" if consent == "no" else "counted: consent missing"
            continue
        tally["respondents"] += 1
        row["outcome"] = "counted"
        times.append(t)
        by_day[kabul_day(t)] += 1

        for d in DIMENSIONS:
            v = val(s, d)
            if d == "main_sector" and not v:
                secs = multi(s, "sectors")
                v = secs[0] if len(secs) == 1 else val(s, "sector_main")
            if d == "ministry" and not v:
                continue                                 # asked only of ministry staff
            dims[d][v or "_blank"] += 1
        for sec in multi(s, "sectors"):
            sectors_any[sec] += 1

        main_sector = val(s, "main_sector") or (multi(s, "sectors")[:1] or [""])[0]
        level = val(s, "level")
        provs = multi(s, "provinces")
        if level == "national":
            provinces["_national"] += 1
            prov_sector["_national"][main_sector or "_blank"] += 1
        elif not provs:
            provinces["_blank"] += 1
        for pv in provs:
            provinces[pv] += 1
            prov_sector[pv][main_sector or "_blank"] += 1
        ministry_gender[val(s, "ministry") or "_none"][val(s, "gender") or "_blank"] += 1

        function = val(s, "function")
        on_main = val(s, "main_path") == "1" if P.get("main_path") else function not in ("", "community")
        path = "main" if on_main else ("community" if function == "community" else "_blank")
        paths[path] += 1
        if val(s, "workplace") == "community" and function not in ("", "community"):
            chw_flag += 1
        if on_main and val(s, "f0_path") == "1":
            scen[val(s, "scen") or "_blank"] += 1

        if on_main:
            ns = val(s, "n_systems")
            if ns:
                n_systems[ns] += 1
            reps = s.get(P.get("systems", "")) or []
            if not isinstance(reps, list):
                reps = []
            names = []
            for item in reps:
                if not isinstance(item, dict):
                    continue
                code = ""
                for k, x in item.items():                 # only the code is read; the rest of the item is dropped
                    if k.split("/")[-1] == "sys_name":
                        code = str(x).strip()
                        break
                if code:
                    names.append(code)
            if names:
                sys_respondents += 1
                sys_instances += len(names)
                for c in names:
                    sys_named[c] += 1
                    if c == "other":
                        sys_other += 1
            sd = val(s, "share_docs")
            if sd:
                share_docs[sd] += 1
            iv = val(s, "interview_ok")
            if iv:
                interview[iv] += 1

        st, en = parse_time(s.get(P.get("start", "start"))), parse_time(s.get(P.get("end", "end")))
        if st and en and timedelta(0) < en - st < timedelta(hours=12):
            minutes = (en - st).total_seconds() / 60
            if path in durations:
                durations[path].append(minutes)
            if path == "main" and minutes < quick:
                quick_n += 1
        else:
            no_duration += 1

    def pct(values: list[float], q: float) -> float | None:
        if not values:
            return None
        v = sorted(values)
        k = (len(v) - 1) * q
        lo, hi = int(k), min(int(k) + 1, len(v) - 1)
        return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)

    def dur_summary(v: list[float]) -> dict:
        return {"n": len(v), "median": round(statistics.median(v), 1) if v else None, "p25": pct(v, 0.25), "p75": pct(v, 0.75)}

    known = {v["uid"]: v for v in form.get("versions") or []}
    version_rows = []
    for uid, n in versions.items():
        info = known.get(uid, {})
        version_rows.append({"uid": uid or "(none)", "deployed": info.get("deployed"), "current": uid == form.get("current_version"), "n": n})
    version_rows.sort(key=lambda r: (r["deployed"] or "", r["uid"]), reverse=True)

    last = max(times) if times else None
    status = {
        "schema": SCHEMA,
        "generated_at": iso(now),
        "form": {"name": form.get("name"), "uid": form.get("uid"), "label_language": form.get("language")},
        "count_from": iso(count_from),
        "submissions": {k: tally.get(k, 0) for k in ("received", "counted", "not_approved", "before_start", "no_time",
                                                     "declined", "consent_missing", "respondents")},
        "recent": {
            "last": iso(last),
            "last_24h": sum(1 for t in times if timedelta(0) <= now - t <= timedelta(hours=24)),
            "last_7d": sum(1 for t in times if timedelta(0) <= now - t <= timedelta(days=7)),
        },
        "by_day": [{"day": d, "n": by_day[d], "declined": declined_by_day.get(d, 0)} for d in sorted(set(by_day) | set(declined_by_day))],
        "dims": {d: dict(c) for d, c in dims.items()},
        "sectors_any": dict(sectors_any),
        "paths": dict(paths),
        "scenarios": dict(scen),
        "provinces": dict(provinces),
        "province_sector": {k: dict(v) for k, v in prov_sector.items()},
        "ministry_gender": {k: dict(v) for k, v in ministry_gender.items()},
        "systems": {
            "respondents": sys_respondents,
            "described": sys_instances,
            "named": dict(sys_named),
            "other": sys_other,
            "n_systems": dict(n_systems),
        },
        "followup": {"share_docs": dict(share_docs), "interview": dict(interview)},
        "quality": {
            "duration_minutes": {k: dur_summary(v) for k, v in durations.items()},
            "quick_minutes": quick,
            "quick": quick_n,
            "no_duration": no_duration,
            "community_workplace_other_role": chw_flag,
            "versions": version_rows,
        },
        "labels": {
            "mode": form["labels"].get("mode", {}),
            "workplace": form["labels"].get("workplace", {}),
            "employer": form["labels"].get("employer", {}),
            "ministry": form["labels"].get("ministry", {}),
            "main_sector": form["labels"].get("main_sector", {}),
            "level": form["labels"].get("level", {}),
            "function": form["labels"].get("function", {}),
            "gender": form["labels"].get("gender", {}),
            "provinces": form["labels"].get("provinces", {}),
            "scen": form["labels"].get("scen", {}),
            "sys_name": form["labels"].get("sys_name", {}),
            "n_systems": form["labels"].get("n_systems", {}),
            "share_docs": form["labels"].get("share_docs", {}),
            "interview": form["labels"].get("interview_ok", {}),
        },
        "targets": cfg.get("targets") or None,
    }
    return status, audit


# --------------------------------------------------------------------------- encryption

def encrypt(plaintext: bytes, password: str) -> bytes:
    """AFGOH1 | salt 16 | nonce 12 | AES-256-GCM ciphertext, key = PBKDF2-SHA256(password, salt, 200 000).

    Salt and nonce are derived from the data, as on the collar study map: the same data and password give the
    same bytes, and different data never reuse a key and nonce pair."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = hashlib.sha256(b"afg-oh-returns-salt" + plaintext).digest()[:16]
    nonce = hashlib.sha256(b"afg-oh-returns-nonce" + salt).digest()[:12]
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS, 32)
    return MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def decrypt(blob: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:6] != MAGIC:
        raise ValueError("not a dashboard data file")
    salt, nonce, ct = blob[6:22], blob[22:34], blob[34:]
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS, 32)
    return AESGCM(key).decrypt(nonce, ct, None)


# --------------------------------------------------------------------------- report

def report(status: dict, audit: list[dict], list_rows: bool) -> None:
    """The run log and step summary carry totals only: the log of a public repository is public."""
    s = status["submissions"]
    lines = [
        f"Submissions read     : {s['received']}",
        f"  not approved       : {s['not_approved']}  (excluded)",
        f"  before count_from  : {s['before_start']}  (excluded)",
        f"  counted            : {s['counted']}",
        f"    respondents      : {s['respondents']}",
        f"    declined consent : {s['declined']}",
        "Form versions        : " + (", ".join(f"{v['uid'][:10]}{' (current)' if v['current'] else ''} x{v['n']}"
                                           for v in status["quality"]["versions"]) or "none"),
    ]
    log("\n".join(lines))
    if list_rows and audit:
        log("\nOne line per submission (no answers):")
        log(f"{'_id':>10}  {'received (Kabul time)':<22}  {'version':<12} {'validation':<13} outcome")
        for a in sorted(audit, key=lambda x: (x["time"] or datetime.min.replace(tzinfo=timezone.utc))):
            when = a["time"].astimezone(KABUL).strftime("%Y-%m-%d %H:%M") if a["time"] else "-"
            log(f"{str(a['id']):>10}  {when:<22}  {str(a['version'])[:10]:<12} {a['validation'] or '-':<13} {a.get('outcome', '')}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### Survey returns\n\n")
            fh.write(f"**{s['respondents']}** respondents so far.\n\n| | |\n|---|---|\n")
            for label, key in [("Submissions read", "received"), ("Counted", "counted"), ("Respondents", "respondents"),
                               ("Declined consent", "declined"), ("Not approved (excluded)", "not_approved"),
                               ("Before count_from (excluded)", "before_start")]:
                fh.write(f"| {label} | {s[key]} |\n")
            fh.write("\nThe breakdowns are on the dashboard, behind the password.\n")


# --------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, "site", "data", "status.json.enc"))
    ap.add_argument("--fixture", help="read submissions from this JSON file instead of KoboToolbox")
    ap.add_argument("--asset", help="with --fixture: read the form from this JSON file")
    ap.add_argument("--password", help="testing only: the password (the workflow uses DASHBOARD_PASSWORD)")
    ap.add_argument("--plain", help="testing only: also write the unencrypted data to this path")
    ap.add_argument("--now", help="override the build time (ISO, UTC) for tests")
    ap.add_argument("--list", action="store_true", help="print one line per submission (no answers)")
    args = ap.parse_args()

    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    if os.environ.get("KOBO_SERVER", "").strip():
        cfg["kobo_server"] = os.environ["KOBO_SERVER"].strip()
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)

    password = args.password if args.password is not None else os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        fail("DASHBOARD_PASSWORD is not set.  Add the dashboard password as a repository secret named "
             "DASHBOARD_PASSWORD (Settings > Secrets and variables > Actions).  Nothing is published without it.")
    if len(password) < 10:
        warn("DASHBOARD_PASSWORD is shorter than 10 characters.  The data file is public, so a short password "
             "can be guessed offline; four or five random words are better.")

    if args.fixture:
        with open(args.fixture, encoding="utf-8") as fh:
            payload = json.load(fh)
        subs = payload["results"] if isinstance(payload, dict) else payload
        with open(args.asset or os.path.join(ROOT, "tests", "fixture_asset.json"), encoding="utf-8") as fh:
            form = read_form(json.load(fh), cfg)
    else:
        token = (os.environ.get("KOBO_TOKEN") or "").strip()
        if not token:
            fail("KOBO_TOKEN is not set.  Add the Kobo API key as a repository secret named KOBO_TOKEN "
                 "(Settings > Secrets and variables > Actions).")
        uid = None
        try:
            uid = find_asset_uid(cfg, token)
            base = cfg["kobo_server"].rstrip("/")
            form = read_form(http_get_json(f"{base}/api/v2/assets/{uid}/?format=json", token), cfg)
            subs = fetch_submissions(cfg, token, uid, form)
        except KoboHTTPError as e:
            fail(explain_http_error(e, uid))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            fail(f"Could not reach KoboToolbox at {cfg['kobo_server']} ({e}).  The previous page stays online.")

    status, audit = build_status(cfg, form, subs, now)
    report(status, audit, args.list)

    plaintext = json.dumps(status, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(encrypt(plaintext, password))
    os.replace(tmp, args.out)
    log(f"\nWrote {os.path.relpath(args.out, ROOT)} ({os.path.getsize(args.out):,} bytes, encrypted)")
    if args.plain:
        if IN_ACTIONS:
            fail("--plain is for local testing only; it would publish the figures unencrypted.")
        with open(args.plain, "w", encoding="utf-8") as fh:
            json.dump(status, fh, ensure_ascii=False, indent=1)
        log(f"Wrote {args.plain} (unencrypted, for local testing)")


if __name__ == "__main__":
    main()
