-- NetHome schedule runner support.
-- Safe to run more than once.

create unique index if not exists schedule_executions_once_idx
    on public.schedule_executions(schedule_id, scheduled_for);

create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;
