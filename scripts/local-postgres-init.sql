-- Local development only: mirrors the PostgreSQL schema allocation (#2).
-- In Azure the schema already exists on the shared Flexible Server; the backend
-- only creates tables inside it.
CREATE SCHEMA IF NOT EXISTS crip AUTHORIZATION crip;
