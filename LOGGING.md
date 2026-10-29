# Reading the logs

How to find out what happened during a GRN submit: what the user did, what the
page showed, and what the server and the Sales Intellect API did. Written after the 28 Oct GRN
incident, when we could not tell whether the user clicked Submit, whether the
page reloaded, or which lines went into which GRN.

The design and the reasoning behind it are in the "GRN Observability Plan"
artifact. This file is the working guide, and the last section lists what is
still to do.

## The four places to look

| Source | Where | Answers | Time zone |
| --- | --- | --- | --- |
| `events.log` | repo root (`/home/ec2-user/app/events.log` on the server) | What the user did, what the page showed, what the server decided. **Start here.** | UTC |
| `timings.log` | repo root | How long each step took, nested under a banner per request | Server local time |
| `actions.log` | repo root | Which product each pasted line was matched to, and why. Also holds the raw paste (`parse_request_received`) | Server local time |
| journal | `journalctl -u sales-intellect-flask` (gunicorn access log) and `-u sales-intellect-streamlit` | Every request that reached Flask: status, size, duration, attempt | UTC (`%(t)s` carries the offset) |

All four are gitignored runtime files. `events.log` and `timings.log` carry the
same `attempt_id`, so one ID follows a submit across all of them.

## The three IDs

Every `events.log` line carries these where they are known.

- **`browser_id`** (`b-…`): stored in a cookie (`si_browser_id`), so it survives reloads and new tabs. Same `browser_id` with a new `session_id` means the same browser reloaded, reconnected or opened a second tab. A different `browser_id` is another person or device.
- **`session_id`** (`s-…`): one per Streamlit session. A reload, websocket reconnect (network drop, laptop sleep), new tab or server restart all mint a new one.
- **`attempt_id`** (`att-…`): one per paste-to-GRN attempt. It starts when the pasted text is committed, and it also goes to Flask as the `X-Request-ID` header, so server events, Sales Intellect API calls and the gunicorn access log (`attempt=…`) carry it too. A new attempt starts when the pasted text changes or after the previous attempt ended with a clean submit.

The text box only commits on blur or Ctrl+Enter, so an attempt starts then, not on the first keystroke.

## Line format

One JSON object per line.

```json
{"ts":"2026-10-29T11:52:55.149Z","level":"WARN","layer":"ui","event":"submit_resumed","session_id":"s-7f3a91","browser_id":"b-19ac","attempt_id":"att-8d41", "...":"event fields"}
```

- `ts`: UTC. `level` is only present for `WARN` and `ERROR`.
- `layer`: `ui` is what the Streamlit page rendered or the user did. `api` is what server/API code decided or returned. One event name is never used by both layers, except `duplicate_submission`, which is logged once by each so you can see both what the server decided and that the page asked.
- `si_call` events are `layer: api` even when the call was made from the Streamlit process (the page's own `GET /shops` and `GET /grn`). They still carry the attempt, so they join up.

## Event reference

### Session and attempt (ui)

| Event | Fields | Meaning |
| --- | --- | --- |
| `session_start` | `ua`, `first_seen` | A new Streamlit session. `first_seen: true` means no browser cookie existed yet |
| `attempt_started` | `text_sha`, `text_chars`, `text_lines` | A paste was committed. `text_sha` is a short hash, not the text |
| `attempt_ended` | `outcome` | The attempt finished with a clean submit (`submitted`) |

### Submit (ui)

| Event | Fields | Meaning |
| --- | --- | --- |
| `submit_clicked` | `source: button_click`, `view` (`paste` or `correction`), `mode`, `shop_id`, `grn_date`, `text_sha`/`lines` | The Submit GRN button returned True on this run |
| `busy_set` / `busy_cleared` | `key`, `held_ms` (on clear) | The busy flag that disables the button while work runs. `key` is e.g. `grn_submit`, `grn_read`, `catalog_refresh` |
| `submit_resumed` | `source: busy_resume`, `note` | The run continued a submit because the busy flag was already set. Normal as the second half of one click |
| `busy_stale` | `busy_age_s`, `prior_attempt_id`, `action: logged_only` | The flag was older than 60 s. Logged only; the submit still continues |
| `refusal_shown` | `message_shown` | The page told the user the server refused the GRN |
| `duplicate_submission` / `duplicate_decision` | see below | The repeat-submit warning and what the user chose |
| `grn_number_found` | `grn_number`, `lines_sent`, `lines_found` | The GRN number, learned from the page's own lookup for the WhatsApp text. `WARN` if the line counts differ |
| `backend_call_failed` | `path`, `status`, `body_head` or `error` | A call from Streamlit to Flask failed (`ERROR`) |

### Server and Sales Intellect API (api)

| Event | Fields | Meaning |
| --- | --- | --- |
| `server_start` | `version` (git SHA), `pid` | A Flask worker started. Several in a row at the same moment means a restart |
| `si_call` | `method`, `path`, `status`, `dur_ms`, `resp_bytes`, `si_request_id` | Every call to the Sales Intellect API. No body is logged on success |
| `si_call_failed` | same, plus `body_head` (first 300 chars) or `error` | A non-2xx or a connection error (`ERROR`). The token is never logged |
| `grn_created` | `shop`, `shop_id`, `grn_date`, `lines_sent`, `si_ack`, `elapsed_ms` | `POST /grn` succeeded. The Sales Intellect API does not return the GRN number, so it is not here; see `grn_number_found` |
| `grn_refused` | `lines_sent`, `lines_complete`, `lines_incomplete`, `incomplete[]` (`text`, `matched_product_id`, `server_reason`) | The server refused the whole GRN because lines had no quantity (`quantity_missing`) or no product (`not_found`). Nothing was created |
| `grn_failed` | `lines_sent`, `status`, `error`, and when the API names a bad line: `rejected_line` (position in the batch, from 0), `rejected_name`, `rejected_detail` | `POST /grn` raised (`ERROR`) |
| `duplicate_submission` | `matches_attempt_id`, `age_s`, `lines`, `action` | The payload is identical to the shop's previous GRN, within 15 minutes. `action` is `asked_user` (a 409 went back) or `submitted_anyway` (the user confirmed) |

## Recipes

Run these from the repo root (`/home/ec2-user/app` on the server). `jq` is needed.

```bash
# Everything one attempt did, in order
jq -c 'select(.attempt_id=="att-8d41")' events.log

# Everything one browser did in a time window
jq -c 'select(.browser_id=="b-19ac" and .ts > "2026-10-28T22:40" and .ts < "2026-10-28T22:55")' events.log

# A readable timeline of one browser
jq -r 'select(.browser_id=="b-19ac") | [.ts, .layer, .event, (.attempt_id // "-")] | @tsv' events.log

# Only what the user saw / only what the server decided
jq -c 'select(.layer=="ui")' events.log
jq -c 'select(.layer=="api")' events.log

# Anything that went wrong
jq -c 'select(.level=="WARN" or .level=="ERROR")' events.log

# Submits that resumed with no click first (the case we could not rule out on 28 Oct)
jq -c 'select(.event=="submit_resumed" and .note)' events.log

# Every refused GRN, with the lines
jq -c 'select(.event=="grn_refused") | {ts, shop, attempt_id, incomplete}' events.log

# Did the page reload around then? Same browser, several sessions
jq -c 'select(.event=="session_start" and .browser_id=="b-19ac")' events.log

# The server's side of the same attempt in the gunicorn access log
journalctl -u sales-intellect-flask --since "2026-10-28 22:40" --until "2026-10-28 22:55" | grep att-8d41

# Timing lines for the same attempt
grep att-8d41 timings.log
```

On the box, `journalctl ... > file` is how to pull a window to download.

## Reading one attempt

A clean submit, in order (times vary, and `si_call` lines are interleaved):

1. `attempt_started`
2. `submit_clicked` (`source: button_click`), then `busy_set`
3. `submit_resumed` (`source: busy_resume`): the page reruns with the flag set
4. `si_call GET /shops`, `si_call POST /grn`, `grn_created` (server)
5. `si_call GET /grn` (the page's lookup, 250 GRNs per page, the slowest step), then `grn_number_found`
6. `attempt_ended`, `busy_cleared`

A refused GRN: `submit_clicked`, `busy_set`, `submit_resumed`, `grn_refused` (api), `refusal_shown` (ui), `busy_cleared`. There is no `grn_created` and no `POST /grn`.

A repeat submit: `duplicate_submission` (api, then ui), and nothing is created. Then one of `duplicate_decision` with `submit_anyway` (followed by `grn_created`) or `cancelled`.

## Answering the questions from the incident

| Question | Look for |
| --- | --- |
| Did the user click Submit? | `submit_clicked` with the attempt's ID. If there is a `submit_resumed` but no `submit_clicked` for that `attempt_id`, the resume carries a `note` and `WARN`: the wrapper acted without a click |
| Did the page reload or reconnect? | `session_start` rows for the same `browser_id`. A new `session_id` between two events is a reload, reconnect or second tab. Several `server_start` at once is a restart |
| Which lines were dropped from which GRN? | None can be dropped any more. `grn_refused.incomplete[]` names the lines that stopped a submit. A GRN that exists has all `lines_sent`; `grn_number_found.lines_found` should equal it |
| Which GRN did this attempt make? | `grn_number_found.grn_number` for the attempt |
| Are two requests the same attempt? | Same `attempt_id` in `events.log`, `timings.log` and the access log |
| Was it a double submit? | `duplicate_submission` for the second one, with `matches_attempt_id` pointing at the first |

## Things that look wrong but are not

- **`submit_clicked` with no `grn_*` event after it.** When the page itself flags lines (no match, or no quantity), it stops before sending anything, and that is not logged yet. See the TODO list. The `/parse` lines in `timings.log` for the same attempt will show the item counts.
- **`submit_resumed` right after every `submit_clicked`.** Normal. The click sets the busy flag and reruns; the rerun does the work.
- **`si_call GET /grn` taking several seconds.** Expected on shops with many GRNs; it is the page's WhatsApp lookup.
- **Two `si_call GET /grn` lines.** The client pages until it gets an empty page, so the second one returns nothing.
- **A 401 on `POST /grn` that is not about the token.** The Sales Intellect API also uses 401 for validation errors, for example `INVALID_VALUE` with `"field":"items[8].product_id"` when a product has stock control turned off. Read the body (`body_head` in `si_call_failed`, or `rejected_detail` in `grn_failed`) before assuming a bad token. The page now shows that line first with the API's reason.
- **`timings.log` times differ from `events.log`.** `timings.log` uses server local time and `events.log` uses UTC.

## Files this adds

`events.py` (writer), `ui_events.py` (Streamlit side: IDs, submit and busy events), `grn_store.py` (the previous-GRN record for the duplicate warning, saved in `last_grn.json`), `deploy/gunicorn.conf.py` (access log format). Tests are in `tests/test_grn_observability.py`.

## TODO

Work still to do, roughly in the order it should happen.

### Soon

- [ ] **Deploy and check on the server.** `deploy/update.sh` pulls and restarts both services. Then confirm: `events.log` appears, the gunicorn access log shows `attempt=att-…` in `journalctl -u sales-intellect-flask`, and `deploy/gunicorn.conf.py` was picked up. None of this has run on the EC2 box yet.
- [ ] **Check the browser cookie in a real browser.** The `browser_id` cookie is set from a small iframe script and is only tested in Streamlit's test harness. Reload a few times and confirm `first_seen` is `true` once and `false` after.
- [ ] **Log what the user was shown (plan change 5).**
  - `parse_result` (api) and `parse_flags_shown` (ui), side by side, with `server_flagged` and `ui_flagged`.
  - `flag_mismatch` (WARN) when they differ.
  - `submit_blocked` when the page stops a submit because of flagged lines. This closes the "click with no outcome" gap above.
- [ ] **Expire the busy flag (plan change 6b).** Today `busy_stale` is only logged. Make it clear the flag without submitting, keep clearing it in a `finally`, and treat a flag from an older `session_id` as stale at once. 60 s is a starting value; choose the real limit from the logged `held_ms` values.
- [ ] **Reverse the preprod test GRN.** `GRN-10055` (GROOMBRIDGE, 1 × Ballet Leggings Black 110) was created on preprod on 29 Oct while testing this.

### Next

- [ ] **Rotation and retention (plan change 7).** `events.log` grows without limit today. Rotate daily and keep at least 90 days (logrotate or a rotating handler), and check disk on the box.
- [ ] **Ship the logs somewhere searchable.** CloudWatch Logs is the suggestion (the app runs on EC2): agent install, an IAM role, a log group with 90-day retention, and a check that the JSON fields show up in Logs Insights. If the `grn_*` events need longer retention, use a second log group filtered on those names.
- [ ] **Make the older logs line up.** Give `timings.log` and `actions.log` a date and UTC, and add `attempt_id` to `actions.log` (it is already in `timings.log`). Raise the Streamlit app logger to INFO, and log the app version once at Streamlit startup (only Flask logs it today).
- [ ] **Tune the duplicate window.** 15 minutes (`GRN_DUPLICATE_WINDOW_S` in `server.py`) is a starting value. Look at how often `duplicate_submission` fires and how often the answer is `submit_anyway`.
- [ ] **Write `attempt_id` into the GRN's notes field**, if the Sales Intellect API has one (for example `via wrapper att-5e09`), so a GRN in Sales Intellect can be traced to its logs and back.
- [ ] **Look for a Sales Intellect request ID.** `si_call` logs `si_request_id` from `X-Request-ID` or `X-Amzn-Trace-Id` if the Sales Intellect API sends one. None showed up in the preprod runs, so it probably sends neither; confirm with the vendor.

### Then

- [ ] **Alerts (plan change 8).**
  - `grn_number_found` with `lines_found != lines_sent`.
  - Three or more `grn_refused` for one shop within 10 minutes.
  - Two `grn_created` for one shop within 15 minutes.
  - `submit_resumed` with a `note`.
  - Any `busy_stale`.
  - Any `flag_mismatch`, once it exists.
  - Any non-2xx on `POST /grn` (`si_call_failed`).
- [ ] **Incident tooling (plan change 9).**
  - `tools/incident_timeline.py` to merge the four sources for a shop and window, grouped by session.
  - A replay tool that runs a logged paste against preprod (needs the raw text kept behind a flag for 30 days; today only its hash is logged).
  - A way to switch `SI_GRN_SKIP_REVIEW` off without a redeploy, to turn off one-click submit during an investigation.
  - A one-page runbook: how to pull the logs, how to reverse a GRN, who to tell, and a post-incident template.

### Maybe

- [ ] **Speed up the page's GRN lookup.** The WhatsApp lookup downloads the shop's first 250 GRNs (about 260 KB and around 7 s on preprod including the empty second page). Ideas: ask for a specific `grn_numbers` value, or check whether the Sales Intellect API returns newest first so a page size of 1 would do. Neither has been tested.
- [ ] **Pick up the unrelated files.** `grn_email.py`, `grn_pdf.py`, `send_grn_email.py` and the `reportlab` line in `requirements.txt` were already uncommitted before this work and were left out of its commit.
