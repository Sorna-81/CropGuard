-- Ensure PostgREST roles can resolve the public schema and the server's scoped
-- SQL role can execute the profile-provisioning helper. RLS remains enabled.
grant usage on schema public to anon, authenticated;
grant select on public.users to authenticated;
grant usage, select on all sequences in schema public to authenticated;
revoke all on function public.claim_or_create_user() from public;
grant execute on function public.claim_or_create_user() to authenticated;
