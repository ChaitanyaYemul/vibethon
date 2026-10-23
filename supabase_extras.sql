do $$ begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    alter publication supabase_realtime add table events;
  end if;
end $$;

alter table users enable row level security;
alter table donors enable row level security;
alter table riders enable row level security;
alter table shelters enable row level security;
alter table shelter_demands enable row level security;
alter table batches enable row level security;
alter table assignments enable row level security;
alter table events enable row level security;
alter table idempotency_keys enable row level security;
alter table app_state enable row level security;

-- Only `events` is readable by the browser (anon) for Realtime. Payloads hold no PII.
create policy "events_read" on events for select to anon using (true);
