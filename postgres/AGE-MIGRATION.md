# Migrating the live cluster to pgvector + Apache AGE

R&D status: **complete & proven.** The image `agentos-pg-age` (built from `Dockerfile.age`) runs
PostgreSQL 16 with **both** pgvector and Apache AGE — proven: openCypher `CREATE`/`MATCH` graph queries
and `vector` similarity in the same database.

The live `agentos-postgres` still runs the plain `pgvector/pgvector:pg16` image (it holds DBOS, audit,
comms, memory, metrics). Because the AGE image is the **same PG16 major**, its data directory is
binary-compatible — migration is just an image swap, no dump/restore:

```bash
cd ~/projects/agent-os/postgres
# 1) (safety) back up the data dir
sudo tar czf pgdata-backup-$(date +%s).tgz pgdata
# 2) point the compose at the AGE image
#    edit docker-compose.yml:  image: agentos-pg-age      (was pgvector/pgvector:pg16)
# 3) recreate (data persists in ./pgdata):
docker compose up -d
# 4) enable the extension:
docker exec -i agentos-postgres psql -U agentos -d agentos -c "CREATE EXTENSION IF NOT EXISTS age;"
```
Then `memory.py` can use real openCypher traversal instead of the recursive-CTE fallback. Caveat
(from research): once AGE is enabled, use dump/restore for future *major*-version upgrades
(`pg_upgrade` is unsupported on AGE databases). Deferred as an ops step — do it when graph workloads
justify it; the capability is built and validated.
