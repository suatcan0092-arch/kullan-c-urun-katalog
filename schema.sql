-- PostgreSQL şeması: DS Damat, Süvari, Altınyıldız Classics, Kiğılı ürünleri
CREATE TABLE IF NOT EXISTS products (
    id              BIGSERIAL PRIMARY KEY,
    brand           TEXT        NOT NULL,
    url             TEXT        NOT NULL UNIQUE,
    sku             TEXT,
    name            TEXT        NOT NULL,
    category        TEXT,
    color           TEXT,
    price           NUMERIC(12,2),
    original_price  NUMERIC(12,2),
    currency        TEXT        DEFAULT 'TRY',
    in_stock        BOOLEAN,
    image_url       TEXT,
    description     TEXT,
    raw             JSONB,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_products_brand    ON products (brand);
CREATE INDEX IF NOT EXISTS idx_products_category ON products (category);
CREATE INDEX IF NOT EXISTS idx_products_sku      ON products (sku);

-- Her çalıştırmada fiyat değişimini izlemek için
CREATE TABLE IF NOT EXISTS price_history (
    id          BIGSERIAL PRIMARY KEY,
    product_id  BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price       NUMERIC(12,2),
    in_stock    BOOLEAN,
    seen_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_price_history_product ON price_history (product_id, seen_at);
