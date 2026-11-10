# NBA Prediction Automation

## Scope and safety

This workflow automates the existing NBA player-prop pipeline. It does not modify projection, probability, calibration, EV, ranking, or qualification formulas. All changes are isolated on `research/ats-feature-study`; no production deployment is performed.

## Pipeline

1. **Slate clock** — Eastern-time slate date remains yesterday until 07:00 ET, then switches to today.
2. **Scheduled refresh** — protected `/api/cron/freeze-daily` refreshes points, rebounds, assists, steals and threes. It calls the existing PropLine/DraftKings event feed, ESPN player logs/identity, positional-defense data and official injury source through existing caches.
3. **Freshness validation** — before freezing, each candidate must have a parseable sportsbook `last_update` timestamp no older than 60 minutes and no more than five minutes in the future. Missing, malformed or stale line timestamps block that market's snapshot.
4. **Projection / market comparison** — existing formulas run unchanged; sportsbook prices are passed to existing probability/EV comparison logic.
5. **Immutable timestamped snapshot** — existing prediction ledger freezes each market's daily candidates. Existing slate snapshots are not overwritten by subsequent refreshes.
6. **Settlement** — protected `/api/cron/settle-pending` is called every three hours by GitHub Actions. This avoids the Hobby-plan Vercel Cron frequency limit.

## Administrative versus public traffic

- Public `GET /api/proprietary-market-board?market=...` serves a saved daily slate or a warm in-process board cache. It does not initiate an expensive upstream market refresh on a cache miss; it returns HTTP 503 with a refresh-required message instead.
- Explicit refresh via `?refresh=true` requires `Authorization: Bearer $CRON_SECRET`.
- `/api/cron/freeze-daily` is the scheduled administrative refresh and requires the same bearer secret. It only proceeds during 07:00–07:59 America/New_York.
- `/api/cron/settle-pending` is also protected by the same bearer secret and performs pending settlement only.
- `GET /api/automation/status` is read-only. It reports saved snapshot counts/timestamps, line freshness and injury-feed metadata; it does not refresh sportsbook/stat sources.
- Dashboard requests must never be used as a refresh trigger. Public reads do not rerun the expensive player-history/workload review; any cached injury annotation is informational and does not re-rank or rewrite the locked pick.

## Cron schedule

`vercel.json` preserves the existing ATS/metrics jobs and schedules the prop slate freeze at 11:00 and 12:00 UTC. The endpoint checks Eastern time, so one trigger matches 07:00 year-round across daylight-saving changes and the other exits harmlessly.

Pending settlement is defined in `.github/workflows/nba-three-hour-settlement.yml` at minute 0 every three hours (`0 */3 * * *`, UTC), with a manual dispatch option. Important GitHub limitation: scheduled workflows execute only from the repository's default branch. Because this workflow is currently isolated on `research/ats-feature-study`, its three-hour schedule is **not active yet**; manual dispatch from the research branch is available after GitHub secrets are configured. Activating the schedule requires moving the scheduler workflow to the default branch or a separate scheduler repository. Do not merge it to the app's production branch as part of this research-only task, because that could trigger a production deployment. The workflow targets the stable Preview branch alias, never the production domain.

### Required one-time configuration for three-hour settlement

1. In Vercel Project Settings → Environment Variables, add `CRON_SECRET` to the **Preview** environment for branch `research/ats-feature-study`. Use the same strong random value you will add to GitHub. Do not paste it into chat or commit it.
2. In Vercel Project Settings → Deployment Protection, create an Automation Bypass Secret for the project. Keep the value private.
3. In GitHub repository Settings → Secrets and variables → Actions → New repository secret, add:
   - `CRON_SECRET` — same value as the Vercel Preview environment variable.
   - `VERCEL_AUTOMATION_BYPASS_SECRET` — the Vercel Automation Bypass Secret.
4. A new Preview deployment must be created after the branch-scoped `CRON_SECRET` was saved; the currently READY Preview predates this environment variable. Once the Vercel deployment limit cooldown has passed, confirm the new Preview is READY, then use the workflow's **Run workflow** action once to verify the protected endpoint returns a successful settlement response.

If either GitHub secret is missing, the job intentionally fails before making a request. A schedule on this non-default research branch will not fire; only manual dispatch is available until the workflow is installed on a repository default branch. If the Vercel Preview secret is missing or different, the endpoint returns HTTP 401. If the Vercel bypass secret is missing or incorrect, Vercel Deployment Protection blocks the request. These checks prevent silently treating an unauthenticated request as a settlement.

## Cost controls and existing cache windows

| Source / work | Existing control |
| --- | --- |
| PropLine events | 60-second fresh cache; stale fallback up to 600 seconds |
| Board result | 180-second in-process cache |
| BettingPros auxiliary data | 300-second cache |
| Tank01 positional defense | 6-hour cache |
| General DVP tables | 1-hour cache |
| API-Sports player positions | 24-hour cache |
| Official injury feed | 120-second cache |
| BALLDONTLIE schedule/status | 10-minute cache; schedule fetch only on the first market |
| ESPN player logs | 30-minute per-player/market cache TTL; failed empty responses are also briefly cached |
| ESPN IDs / rosters | existing per-process caches |
| Negative injury/workload review | after provisional Top 5 only; replacements checked as needed |
| Settlement | scheduled every three hours, not triggered by dashboard requests |

Vercel serverless instances are ephemeral: in-memory caches reduce duplicate calls within a warm instance but are not a durable shared cache. The database-backed frozen slate is the durable public read path.

## Errors and alerts

- Market-level failures are isolated; one failed market does not erase the other markets' result.
- Missing, malformed or stale line timestamps block the affected market's daily freeze.
- Cron response reports completed/partial status and per-market errors.
- If `ALERT_WEBHOOK_URL` is configured, a warning is posted when one or more markets fail to freeze. Webhook delivery failure does not hide the original refresh outcome.
- Configure `CRON_SECRET` in Vercel Preview (research branch) and Production only when production rollout is explicitly approved. Optionally configure `ALERT_WEBHOOK_URL` to a private alert receiver. Never put credentials in GitHub source.
- Status endpoint does not expose API keys.

## Deployment checklist

1. Configure `CRON_SECRET` in Vercel Preview for `research/ats-feature-study`.
2. Configure GitHub Actions secrets `CRON_SECRET` and `VERCEL_AUTOMATION_BYPASS_SECRET`.
3. Run the GitHub settlement workflow manually and verify a successful response.
4. Verify unauthenticated `refresh=true` returns 401.
5. Verify public cache misses do not contact PropLine/ESPN upstream services.
6. Verify stale/missing line timestamps prevent the snapshot from being frozen.
7. Confirm all five market snapshots are timestamped in the ledger and cron invocations appear in logs before considering production promotion.

## Limitations

This is code and cron configuration on the research branch. It does not set Vercel environment variables, create GitHub secrets, provision an alert webhook, or prove that production cron jobs are running. The public read path may return 503 if no snapshot/cache is available; this is intentional fail-closed behavior rather than silently spending API quota or inventing a board.

## Repository validation

The GitHub Actions workflow at `.github/workflows/nba-automation-validation.yml` runs Python syntax checks for the API and tracker modules and the tracker regression tests on pushes and pull requests to this research branch. It does not deploy the app or contact production APIs.
