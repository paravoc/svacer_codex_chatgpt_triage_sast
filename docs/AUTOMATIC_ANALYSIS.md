# Budgeted local analysis

The optional `app/analysis_campaign.py` supervisor operates only on explicitly listed
local jobs and marker IDs. It never publishes to Svacer, changes the model, reads
credential files, or assigns a verdict in place of source evidence.

The current campaign and status are local files under `RESULTS/analysis-campaign*.json`.
They are excluded from Git. Existing decisions, history and queues are backed up before
the initial selection is extended. Jobs run in the configured order; each job retains
its own selected model and parallel-worker setting.

## Allowance protection

The supervisor reads `account/rateLimits/read` every 15 seconds through the existing
Codex app-server integration. `usedPercent` is consumed allowance, not remaining
allowance; the tightest primary/secondary Codex window is used. The field semantics
are documented in the [official app-server reference](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt).

For a requested floor of 55%, the stop threshold is 60%, leaving five percentage
points of headroom. No new model work is started at or below that threshold. Active
work is stopped with its results, drafts and reservations retained. Missing, malformed
or unavailable allowance data also stops the campaign. These stops are latched, not
automatically undone after quota recovers. No credits are purchased or resets redeemed.

Guarded runners check a 60-second supervisor lease before launching and while running.
If the supervisor exits unexpectedly, new guarded runners stop rather than continuing
without budget supervision. An already-running older process needs a normal restart
to gain this intrinsic lease check; the supervisor can still stop it externally.

This is not a server-side hard spending cap: other account activity, in-flight work
and delayed usage reporting can consume allowance between checks. The reserve reduces
that risk but cannot mathematically guarantee an account-wide percentage floor.

## Recovery without infinite retries

- Existing provider retries are local to one marker and bounded to two retries.
- The campaign allows at most one additional launch for recorded transient failures;
  markers with only missing-proof gaps remain saved and are not blindly repeated.
- A pending verifier with a recorded string-array serialization error in the current
  launch's worker journal can use that same single restart allowance. The saved
  primary result is retained; verified/challenged checks and stale journals are excluded.
  Selected Confirmed records awaiting verification resume only that stage, including
  when an earlier recheck flag is still present. Parallel verifiers own separate notes.
- A new worker stops after 10 minutes without protocol events, 30 minutes per model
  turn, or 45 minutes total in that worker invocation. Saved evidence is retained.
- If all active workers are silent for 12 minutes, or preparation has no worker for
  15 minutes, the supervisor requests a stop and permits one bounded restart.
- A user-requested immediate or graceful stop is respected. Queue removal or a changed
  launch is not permission to expand the automatic campaign.
  A later explicit desktop/web Start can detach that one idle job from a `user_stopped`
  campaign after its supervisor exits. The campaign itself remains stopped. Its stop
  threshold (60% in the example above) is transferred only if the job has no percentage
  setting yet; an existing user setting, including 0/off, is kept for the new manual run.
  The UI reports the resulting setting. The old guard is
  retained with a manual-resume audit, not deleted. The new runner checks fresh quota
  before model work and throughout analysis. Automatic retries, quota/error stops,
  unknown campaign state and live supervisors cannot use this handover.
- An empty queue is not completion if its selected markers still lack results or have
  a challenged verification. Those records remain `needs_attention`.

Unresolved semantic evidence gaps require investigation, not a forced False Positive,
Confirmed or Won't Fix. Technical supervision does not eliminate that evidence boundary.

## User-configurable remaining percentage

Desktop: **Настройки → Останавливать при остатке Codex → Применить**.
Web: **Останавливать при остатке Codex → Сохранить порог**.
The per-project `codex_min_remaining_percent` setting accepts integers 0–99.
For example, 20 means stop at **20% remaining or less**, not after using 20%
and not after consuming a per-run token budget. Zero disables this optional guard;
existing account/campaign guards remain independent and may be stricter.

The child runner uses `account/rateLimits/read` before preparation/model work,
then every 15 seconds even when the UI is closed. It checks the tightest available
core Codex window as `100 - usedPercent`; missing/malformed quota is unknown,
not unlimited. The format follows the [official App Server documentation](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt).

Settings are re-read at each check, so changes take effect within one polling
interval on an updated runner. Saving never starts/resumes analysis. Old running
processes need a restart to load the feature. On threshold crossing or unavailable
quota, owned model processes stop via normal runner supervision; existing results,
history, drafts and queue reservations are retained. The stop is latched for that
launch, and the campaign must not restart it. Another manual start rechecks actual
account quota: it never resets the account percentage or the configured threshold.
No resets or credits are consumed. Usage reporting, concurrent account activity and
requests already in flight mean this cannot guarantee an exact account-wide floor.
