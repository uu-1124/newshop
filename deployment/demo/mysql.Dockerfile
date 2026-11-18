FROM python:3.13.5-slim-bookworm@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419 AS seed
WORKDIR /seed
COPY deployment/demo/build-seed.py deployment/demo/seed.sql ./
COPY deployment/init/01-mall.sql ./upstream.sql
RUN python build-seed.py upstream.sql > /01-mall.sql

FROM mysql:8.0.35@sha256:c6812f0dcd978b589d9d3b90c20bd5e4a799deab4fd492985e0a076c5be1ee40
COPY --from=seed /01-mall.sql /docker-entrypoint-initdb.d/01-mall.sql
COPY deployment/init/agent-*.sql /docker-entrypoint-initdb.d/
