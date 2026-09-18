-- Run this in the NetHome AC Control Supabase SQL Editor
-- AFTER storing the existing Vercel NETHOME_API_TOKEN in Supabase Vault
-- under the secret name: nethome_api_token
--
-- The secret value itself is intentionally not stored in GitHub.

create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;

create unique index if not exists schedule_executions_once_idx
    on public.schedule_executions(schedule_id, scheduled_for);

do $$
begin
  if exists (select 1 from cron.job where jobname = 'nethome-schedule-runner') then
    perform cron.unschedule('nethome-schedule-runner');
  end if;
end $$;

select cron.schedule(
  'nethome-schedule-runner',
  '* * * * *',
  $job$
  select net.http_post(
    url := 'https://nethome-web-control-six.vercel.app/api/automation/run',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization',
      'Bearer ' || (
        select decrypted_secret
        from vault.decrypted_secrets
        where name = 'nethome_api_token'
        limit 1
      )
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 50000
  );
  $job$
);

select jobid, jobname, schedule, active
from cron.job
where jobname = 'nethome-schedule-runner';
