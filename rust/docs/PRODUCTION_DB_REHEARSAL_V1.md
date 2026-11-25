# Production database recovery rehearsal (2026-11-25)

Read-only production snapshot; root-private local logical archive under /4t/vr-db-rehearsal.d9x_uj39. Do not distribute or commit the archive: it includes original credentials and user data. Database only, not media files, PostgreSQL cluster roles, privileges or PITR archive. Same local PostgreSQL 18.4 / pgvector 0.8.4 image; no network in disposable clone, no application service started. Original PostgreSQL remained untouched.

- Backup 834474444 bytes in 42.7s, SHA-256 verified on reuse. Source counts and dump from same exported snapshot.
- First restore failed on early startup readiness, second exceeded 300s. Both clones removed. Final parallel restore (4 jobs, read-only archive bind, 1800s deadline) completed in 654.94s.
- Exact count for each of 35 original tables, 1523445 total rows; 120 indexes and 424 constraints matched, no invalid indexes or unvalidated constraints. Counts and catalog inventory are not a per-row content hash or independent media check.
- Recreated recorded libc 2.41 vs actual 2.36 in clone. REINDEX DATABASE then ALTER DATABASE REFRESH COLLATION VERSION; both 2.36 afterward, 100.62s, original row counts unchanged. No production collation change.
- All 39 SQLx migrations on restored real data in 32.77s, all original table counts unchanged, zero vr2 physical tables. Strict preflight blocked 5083 legacy tasks, 355 legacy workflow records, 60 uncertain publications; legacy operations/render execution blockers zero. No ownership transfer or publication.
- Latest Rust code adds read-only database collation check in both standalone preflight modes and bulk apply; native deployment smoke passed deliberate mismatch and no implicit repair, seven native processes plus nginx in synthetic isolated database. Full data rehearsal ran against the preceding binary 6d9c148; new gate tested separately. No new migration.
- Clone and labeled volumes removed. Private report and backup retained. Source metadata rechecked at restore time, counts from original snapshot. Re-run only with explicitly approved local backup, safe disk capacity, no concurrent migration and no deployment. This is not an off-host backup, and does not prove actual production maintenance duration.

Script: rust/scripts/rehearse-production-db.py (root only, explicit confirmation, no data/log output beyond summary).
