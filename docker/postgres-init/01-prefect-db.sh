#!/bin/sh
# Creates the Prefect database next to the aibrarian one on first start.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
  CREATE DATABASE prefect OWNER "$POSTGRES_USER";
  \c $POSTGRES_DB
  CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
