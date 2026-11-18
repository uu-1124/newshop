FROM python:3.13.5-slim-bookworm AS seed
WORKDIR /seed
COPY deployment/demo/build-seed.py deployment/demo/seed.sql ./
COPY deployment/init/01-mall.sql ./upstream.sql
RUN python build-seed.py upstream.sql > /01-mall.sql

FROM mysql:8.0.35
COPY --from=seed /01-mall.sql /docker-entrypoint-initdb.d/01-mall.sql
COPY deployment/init/agent-*.sql /docker-entrypoint-initdb.d/
