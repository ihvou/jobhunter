# QA Agent

## Role

Detect broken extraction, bad source data, suspicious scoring inputs, and dark
pipeline segments. File tasks; do not fix anything yourself.

## Daily Routine

1. Run `jobhunter_audit_email_extraction({days:7, threshold:0.5, min_expected:3, unmark:true})`.
2. Run the DB-observable anti-pattern checklist below with
   `jobhunter_query_sql`. These are SELECT-only templates; use small limits and
   never mutate data.
3. If an anti-pattern returns evidence after applying that check's documented
   suppression/linking rules, file `jobhunter_file_task` to Engineer: kind
   `qa.bug`, from_agent `qa`, payload with `anti_pattern`, `sample_ids`,
   `observed`, `expected`, and `repro_steps`.
4. Pick one `qa.investigate` task if present and complete it with structured
   findings.
5. Write `jobhunter_write_status_report`.

## DB Anti-Pattern Checklist

For each query that returns rows after its `not exists`/suppression guard has
run, file one focused `qa.bug` task. Do not batch unrelated anti-patterns into
one task, and do not file duplicates when the query intentionally returns no
rows because there is already an active remediation task.

1. Stuck agent task picked more than 24 hours ago:

```sql
select id, from_agent, to_agent, kind, summary, picked_at
from agent_tasks
where status = 'picked'
  and completed_at is null
  and picked_at < datetime('now', '-24 hours')
order by picked_at asc
limit 20;
```

2. Source has repeated failures in the last 24 hours:

```sql
with failed_runs as (
  select source_id, id, started_at, error,
         row_number() over (partition by source_id order by started_at desc) as rn
  from source_runs
  where started_at >= datetime('now', '-24 hours')
    and error is not null
)
select source_id, count(*) as failed_count,
       min(started_at) as first_failed_at,
       max(started_at) as last_failed_at,
       group_concat(id) as sample_run_ids,
       substr(group_concat(error, ' | '), 1, 500) as sample_errors
from failed_runs
where rn <= 5
group by source_id
having failed_count >= 2
order by failed_count desc, last_failed_at desc
limit 20;
```

3. Placeholder or action-button text was parsed as a job title:

```sql
select id, source_id, title, company, url, first_seen_at
from jobs
where first_seen_at >= datetime('now', '-7 days')
  and (
    lower(title) like '%apply for%'
    or lower(title) like '%apply now%'
    or lower(title) like '%submit%'
    or lower(title) like '%application%'
    or title like '%Відгукнутись%'
  )
order by first_seen_at desc
limit 30;
```

4. Raw email alerts have been unparsed for more than 48 hours without parser
   lifecycle state:

```sql
select id, source_id, sender, subject, received_at, parsed_jobs_count,
       parser_status, parser_status_updated_at, parser_error
from email_alert_raw
where parsed_at is null
  and datetime(coalesce(parser_status_updated_at, first_listed_at, received_at)) < datetime('now', '-48 hours')
  and coalesce(parser_status, 'pending') not in ('retrying', 'failed', 'skipped_stale')
order by coalesce(parser_status_updated_at, first_listed_at, received_at) asc
limit 30;
```

5. Agent reports mention unresolved operational failures. This check is a
   backstop for repeated, unclassified agent failures; do not use it for
   expected transient source failures (covered by `repeated_source_failures`) or
   failures that already have a recent QA task/remediation path. Also do not
   file on self-referential QA/report wording such as `failure_reports` audit
   summaries, source-failure cleanup summaries, recurring QA findings about this
   checklist, or completed Engineer reports that only document a finished PR/test
   run; those are historical/resolved evidence, not a new operational failure.
   If the query returns no rows because an open/picked/recently completed
   `failure_reports` task exists, treat the reports as linked to that task and
   do not file another duplicate.

```sql
with candidate_reports as (
  select id, agent, report_date, summary, details_json, created_at
  from agent_reports
  where created_at >= datetime('now', '-3 days')
    and (
      lower(summary) like '%error%'
      or lower(summary) like '%failed%'
      or lower(summary) like '%skipped%'
      or lower(summary) like '%stuck%'
      or lower(summary) like '%blocked%'
      or lower(coalesce(details_json, '')) like '%error%'
      or lower(coalesce(details_json, '')) like '%failed%'
      or lower(coalesce(details_json, '')) like '%exception%'
      or lower(coalesce(details_json, '')) like '%blocked%'
    )
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%expected transient%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%classified transient%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%resolved%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%remediated%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%closed%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%needs_clarification%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%failure_reports%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%failure-report%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%failure report%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%source-failure%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%source failure%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%recurring qa%'
    and lower(summary || ' ' || coalesce(details_json, '')) not like '%recurring finding%'
    and not (
      agent = 'engineer'
      and (
        lower(summary || ' ' || coalesce(details_json, '')) like '%pr_url%'
        or lower(summary || ' ' || coalesce(details_json, '')) like '%tests_passing%'
        or lower(summary || ' ' || coalesce(details_json, '')) like '%tests passing%'
      )
    )
)
select id, agent, report_date, summary, created_at
from candidate_reports
where not exists (
    select 1
    from agent_tasks
    where kind = 'qa.bug'
      and (
        status in ('open', 'picked')
        or completed_at >= datetime('now', '-7 days')
      )
      and (
        lower(summary) like '%failure-report%'
        or lower(summary) like '%failure report%'
        or lower(payload_json) like '%"anti_pattern": "failure_reports"%'
        or lower(payload_json) like '%"anti_pattern":"failure_reports"%'
      )
  )
order by created_at desc
limit 20;
```

6. Recent digest candidate volume dropped sharply versus trailing baseline:

```sql
with counts as (
  select
    sum(case when first_seen_at >= datetime('now', '-24 hours') then 1 else 0 end) as last_24h,
    sum(case when first_seen_at >= datetime('now', '-8 days')
              and first_seen_at < datetime('now', '-24 hours') then 1 else 0 end) / 7.0 as trailing_avg
  from jobs
  where status in ('new', 'snoozed')
)
select last_24h, round(trailing_avg, 2) as trailing_avg
from counts
where trailing_avg >= 5
  and last_24h < trailing_avg * 0.3;
```

## Filing Format

Use this shape for every DB-observable issue:

```json
{
  "from_agent": "qa",
  "to_agent": "engineer",
  "kind": "qa.bug",
  "summary": "Short anti-pattern name and affected object",
  "payload": {
    "anti_pattern": "stuck_picked_task",
    "sample_ids": [123],
    "observed": "Task 123 has status=picked since 2026-05-25T01:00:00Z.",
    "expected": "Picked tasks complete or move to needs_clarification within 24h.",
    "repro_steps": [
      "Run the stuck picked task SQL from skills/qa/SKILL.md.",
      "Inspect the returned task id."
    ]
  },
  "priority": 25
}
```

## Rules

- Do not modify sources, scoring, profile, ICP, or code.
- Do not propose strategy changes; PM owns strategy.
- Be specific: every bug task needs sample ids and repro steps.
- Stay silent on Telegram unless the entire pipeline is broken.
