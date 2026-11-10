# NBA Prediction Automation

## Scope and safety

This workflow automates the existing NBA player-prop pipeline. It does not modify projection, probability, calibration, EV, ranking, or qualification formulas. All changes are isolated on `research/ats-feature-study`; no production deployment is performed.

## Pipeline

1. **Slate clock** — Eastern-time slate date remains yesterday until 07:00 ET, then switches to today.
2. **Scheduled refresh** — protected `/api/cron/freeze-daily` refreshes points, rebounds, assists, steals and threes. It calls the existing PropLine/DraftKings event feed, ESPN player logs/identity, positional-defense data and official injury source through existing caches.
3. **Freshness validation** — before freezing, each candidate must have a parseable sportsbook `last_update` timestamp no older than 60 minutes and no more than five minutes in the future. Missing, malformed or stale line timestamps block that market's snapshot.
4. **Projection / market comparison** — existing formulas run unchanged; sportsbook prices are passed to existing probability/EV comparison logic.
5. **Immutable timestamped snapshot** — existing prediction ledger freezes each market's daily candidates. Existing slate snapshots are not overwritten by subsequent refreshes.
6. **Settlement** — protected `/api/cron/settle-pending` runs on a controlled schedule.

## Administrative versus public traffic

- Public `GET /api/proprietary-market-board?market=...` serves a saved daily slate or a warm in-memory board cache. It does not initiate an expensive upstream market refresh on a cache miss; it returns HTTP 503 with a refresh-required message instead.
- Explicit refresh via `?refresh=true` requires `Authorization: Bearer $CRON_SECRET`.
- `/api/cron/freeze-daily` is the scheduled administrative refresh and requires the same bearer secret. It only proceeds during 07:00–07:59 America/New_York.
- `GET /api/automation/status` is read-only. It reports saved snapshot counts/timestamps, line freshness and injury-feed metadata; it does not refresh sportsbook/stat sources.
- Dashboard requests must never be used as a refresh trigger.

## Cron schedule

`vercel.json` preserves existing ATS/metrics jobs and adds:
- Prop slate freeze at 11:00 and 12:00 UTC. The endpoint checks Eastern time, so one trigger matches 07:00 year-round across daylight-saving changes and the other exits harmlessly.
- Pending settlement every three hours from 03:00 through 18:00 UTC.

Vercel Cron schedules are UTC. Configure `CRON_SECRET` in the Vercel project so Cron requests carry the bearer authorization.

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
| ESPN player logs / IDs / rosters | existing per-process caches |
| Negative injury/workload review | after provisional Top 5 only; replacements checked as needed |
| Settlement | scheduled rather than triggered by every dashboard request |

Vercel serverless instances are ephemeral: in-memory caches reduce duplicate calls within a warm instance but are not a durable shared cache. The database-backed frozen slate is the durable public read path.

## Errors and alerts

- Market-level failures are isolated; one failed market does not erase the other markets' result.
- Missing, malformed or stale line timestamps block the affected market's daily freeze.
- Cron response reports completed/partial status and per-market errors.
- If `ALERT_WEBHOOK_URL` is configured, a warning is posted when one or more markets fail to freeze. Webhook delivery failure does not hide the original refresh outcome.
- Configure `CRON_SECRET` and optionally `ALERT_WEBHOOK_URL` as Vercel environment variables. Never put credentials in GitHub source.
- Status endpoint does not expose API keys.

## Deployment checklist

1. Configure `CRON_SECRET` in Vercel Production (and Preview if testing cron there).
2. Optionally configure `ALERT_WEBHOOK_URL` to a private alert receiver.
3. Deploy this branch to Preview and test the protected freeze with a bearer secret.
4. Verify unauthenticated `refresh=true` returns 401.
5. Verify public cache misses do not contact PropLine/ESPN upstream services.
6. Verify stale/missing line timestamps prevent the snapshot from being frozen.
7. Confirm all five market snapshots are timestamped in the ledger and cron invocations appear in Vercel logs before considering production promotion.

## Limitations

This is code and cron configuration on the research branch. It does not set Vercel environment variables, provision an alert webhook, deploy the branch, or prove that production cron jobs are running. The public read path may return 503 if no snapshot/cache is available; this is intentional fail-closed behavior rather than silently spending API quota or inventing a board.
