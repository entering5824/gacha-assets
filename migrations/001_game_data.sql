-- Independent, additive migration. Run explicitly as a database owner.
-- Asset Contract v1 and Storage bucket configuration are untouched.
begin;

create table if not exists public.game_data_entities (
  game text not null,
  locale text not null,
  canonical_id text not null,
  content_hash text not null check (content_hash ~ '^[a-f0-9]{64}$'),
  entity jsonb not null,
  primary key (game, locale, canonical_id, content_hash)
);
create table if not exists public.game_data_releases (
  sync_id text primary key check (sync_id ~ '^[a-f0-9]{64}$'),
  game text not null,
  locale text not null,
  descriptor jsonb not null,
  created_at timestamptz not null default now(),
  unique (game, locale, sync_id)
);
create index if not exists game_data_entities_by_hash
  on public.game_data_entities (game, locale, content_hash);
create table if not exists public.game_data_active (
  game text not null,
  locale text not null,
  sync_id text not null,
  targets jsonb not null,
  activated_at timestamptz not null default now(),
  primary key (game, locale),
  foreign key (game, locale, sync_id) references public.game_data_releases (game, locale, sync_id)
);
create table if not exists public.game_data_sync_runs (
  sync_id text primary key,
  report jsonb not null,
  updated_at timestamptz not null default now()
);
alter table public.game_data_entities enable row level security;
alter table public.game_data_releases enable row level security;
alter table public.game_data_active enable row level security;
alter table public.game_data_sync_runs enable row level security;

create policy game_data_active_read on public.game_data_active for select to anon, authenticated using (true);
create policy game_data_release_read on public.game_data_releases for select to anon, authenticated
  using (exists (select 1 from public.game_data_active a where a.sync_id = game_data_releases.sync_id));
create policy game_data_entity_read on public.game_data_entities for select to anon, authenticated
  using (exists (
    select 1 from public.game_data_active a
    join public.game_data_releases r on r.sync_id = a.sync_id
    where a.game = game_data_entities.game and a.locale = game_data_entities.locale
      and r.descriptor->'entities' @> jsonb_build_array(jsonb_build_object(
        'canonicalId', game_data_entities.canonical_id, 'contentHash', game_data_entities.content_hash))
  ));
grant select on public.game_data_active, public.game_data_releases, public.game_data_entities to anon, authenticated;
grant all on public.game_data_entities, public.game_data_releases, public.game_data_active, public.game_data_sync_runs to service_role;

create or replace function public.activate_game_data_release(
  p_game text, p_locale text, p_sync_id text, p_expected text, p_targets jsonb
) returns void language plpgsql security invoker set search_path = public as $$
declare current_sync text;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_game || ':' || p_locale, 0));
  select sync_id into current_sync from public.game_data_active where game = p_game and locale = p_locale;
  if current_sync is not distinct from p_sync_id then return; end if;
  if current_sync is distinct from p_expected then raise exception 'active release changed; verify and retry'; end if;
  if p_targets->'supabase'->>'status' is distinct from 'verified'
     or p_targets->'mongodb'->>'status' is distinct from 'verified' then
    raise exception 'both destinations must be verified';
  end if;
  if not exists (select 1 from public.game_data_releases where sync_id = p_sync_id and game = p_game and locale = p_locale) then
    raise exception 'release not staged';
  end if;
  insert into public.game_data_active (game, locale, sync_id, targets)
    values (p_game, p_locale, p_sync_id, p_targets)
    on conflict (game, locale) do update set sync_id = excluded.sync_id, targets = excluded.targets, activated_at = now();
end $$;
revoke all on function public.activate_game_data_release(text,text,text,text,jsonb) from public, anon, authenticated;
grant execute on function public.activate_game_data_release(text,text,text,text,jsonb) to service_role;

create or replace function public.record_game_data_sync_run(p_sync_id text, p_report jsonb)
returns void language sql security invoker set search_path = public as $$
  insert into public.game_data_sync_runs (sync_id, report) values (p_sync_id, p_report)
  on conflict (sync_id) do update set report = excluded.report, updated_at = now();
$$;
revoke all on function public.record_game_data_sync_run(text,jsonb) from public, anon, authenticated;
grant execute on function public.record_game_data_sync_run(text,jsonb) to service_role;
commit;
