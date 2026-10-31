-- Supabase SQL Editor'de çalıştırın
create table if not exists public.products (
  id bigserial primary key,
  brand text not null,
  url text not null unique,
  sku text,
  name text not null,
  category text,
  color text,
  price numeric(12,2),
  original_price numeric(12,2),
  currency text default 'TRY',
  in_stock boolean,
  image_url text,
  description text,
  raw jsonb,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);
create index if not exists idx_products_brand on public.products (brand);
create index if not exists idx_products_category on public.products (category);

alter table public.products enable row level security;
-- Dashboard (anon anahtar) sadece okuyabilir; yazma yalnızca service_role ile yapılır.
drop policy if exists "public read" on public.products;
create policy "public read" on public.products for select to anon using (true);
