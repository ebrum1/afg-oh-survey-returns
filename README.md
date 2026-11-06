# Afghanistan One Health survey returns

**Live page: https://ebrum1.github.io/afg-oh-survey-returns/** (opens with the team password)

This repository publishes a one-page dashboard of the returns to the stakeholder survey of the One Health information platform mapping, collected through KoboToolbox.  It shows how many people have answered, day by day against the cut-off, and who they are: by ministry, sector, level, province, role, gender, way of answering and scenario version.  It also shows the data systems named, the offers of documents and interviews, and checks on the returns.  It exists so that the assessment team can follow the fieldwork without exporting data.

The page updates itself every hour from 07:05 to 20:05, Kabul time.  Nobody needs to run anything.

## How it works

1. A scheduled GitHub Action (`.github/workflows/update-dashboard.yml`) runs `scripts/update.py`.
2. The script finds the survey in KoboToolbox by its form ID, `afg_oh_mapping_survey`, with the API key held in the repository secret `KOBO_TOKEN`.  It reads the deployed form to learn where each field sits and what each answer code is called.  It then requests **only** the fields it counts: submission time, start and end time, form version, validation status, consent, way of answering, the profile questions in section B, the routing results, the number of systems and the systems named (D1–D2), and the two follow-up offers (D15, I5).  It never requests names, telephone numbers, email addresses, comments or any other answer.  KoboToolbox can only return the D2 systems repeat whole, so the script keeps the system codes and drops everything else in it, in memory.
3. The script writes counts only to `site/data/status.json.enc`, encrypted with the repository secret `DASHBOARD_PASSWORD`: AES-256-GCM, with a key derived by PBKDF2-SHA256 over 200 000 rounds, the scheme of the collar study map.  The Action checks that the site holds nothing but the page and the encrypted file, then publishes the `site` folder to GitHub Pages.  If KoboToolbox cannot be reached, the run fails and the previous page stays online.
4. The page asks for the password, fetches the encrypted file and decrypts it in the browser.  The password never leaves the device.  "Remember on this device" keeps it in that browser until someone clicks "Forget the password on this device".

## What is public

Anyone with the link can load the page and download the encrypted file, because GitHub Pages sites are public.  Without the password the file cannot be read.  This repository holds code and synthetic test data only; survey data are never committed.  The run logs of a public repository are public too, so they carry totals only.

Because the encrypted file can be attacked offline, the password should be long: four or five random words are better than a short word with symbols.  Share it only within the assessment team.  Even counts can point to a person when a group is small.

## Common tasks

**Update the page now.**  Open the **Actions** tab, choose **Update dashboard**, then **Run workflow**.  Tick "Also print one line per submission" to list, in the log, each submission's ID, time, form version and validation status (no answers).

**Leave a test submission out of the count.**  In KoboToolbox, open the project's data table and set the submission's validation status to **Not approved**.  The next update leaves it out, and the page says how many it has left out.

**Count only submissions received after a start date.**  Set `count_from` in `config.json`, for example `"count_from": "2026-11-03T00:00:00+04:30"`.  Leave it as `null` to count everything.

**Move the cut-off.**  Change `close_date` in `config.json`.

**Show progress against targets.**  Add the targets to `config.json`, for example `"targets": {"ministry": {"moph": 40, "mail": 30, "nepa": 6}}`.  The breakdown then shows "n of target" for each listed answer.  Targets work for `ministry`, `main_sector`, `level`, `function`, `employer`, `workplace` and `gender`; the codes are the answer names in the XLSForm.

**Change the password.**  Open **Settings → Secrets and variables → Actions**, edit `DASHBOARD_PASSWORD`, then run the workflow.  Tell the team: the saved password stops working, and the page asks for the new one.

**Replace the Kobo API key.**  Edit the secret `KOBO_TOKEN` in the same place.  A Kobo API key opens every project its account can see, so a separate Kobo account with view-only access to this project is the safer choice.

**If the survey moves to another project or server.**  Set the repository variable `KOBO_ASSET_UID` (**Settings → Secrets and variables → Actions → Variables**) to the project's ID, the part of its KoboToolbox address after `/forms/`.  Set `KOBO_SERVER` if the project is not on `https://kf.kobotoolbox.org`, for example `https://eu.kobotoolbox.org`.

**If the form changes.**  The field names the script counts are listed under `fields` in `config.json`.  The script finds each one by name in the deployed form, whatever group it sits in, and warns in the log if a field has disappeared.

## If the page stops updating

The page shows a warning when its figures are more than 14 hours old.  GitHub also emails the repository owner when a run fails.  Open the **Actions** tab and read the failed run; the error line says what went wrong.  The usual causes are:

- **HTTP 401 or 403**: the Kobo API key has changed or expired.  Replace `KOBO_TOKEN`.
- **HTTP 404**: the account behind the key can no longer view the project's submissions, or `KOBO_ASSET_UID` is wrong.
- **"No project with form ID afg_oh_mapping_survey"**: the account behind the key cannot see the project.  Share the project with that account, or set `KOBO_ASSET_UID`.
- **"KOBO_TOKEN is not set"** or **"DASHBOARD_PASSWORD is not set"**: a secret is missing.  Nothing is published without the password.
- **Scheduled runs have stopped**: GitHub suspends schedules in public repositories after 60 days without activity.  The workflow re-enables itself on every scheduled run to prevent this, but if it has been suspended, open **Actions → Update dashboard** and click **Enable workflow**.

## Files

| Path | Purpose |
|---|---|
| `site/index.html` | The page.  Plain HTML, CSS and JavaScript; no external libraries; decryption with the browser's own Web Crypto. |
| `scripts/update.py` | Reads KoboToolbox, counts, encrypts and writes the data file.  Python standard library plus `cryptography`. |
| `config.json` | Kobo server, form ID, field names, cut-off, exclusions, scenario labels and optional targets. |
| `tests/` | Offline tests (`python3 -m unittest discover -s tests`) on synthetic data; `make_fixtures.py` rebuilds that data from the XLSForm. |
| `.github/workflows/update-dashboard.yml` | The hourly schedule, the publishing check and the Pages deployment. |

To test locally without KoboToolbox:

```
python3 -m pip install -r requirements.txt
python3 scripts/update.py --fixture tests/fixture_submissions.json --asset tests/fixture_asset.json --password "test password"
cd site && python3 -m http.server 8000
```

Then open `http://localhost:8000` and enter `test password`.
