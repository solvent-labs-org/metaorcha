-- Story 2.3 — settle idempotency per run_id (AR-11).
--
-- `settled_run_id` mirrors `run_id` on a settled row and stays NULL on every
-- refused row. Postgres treats NULLs as distinct in a unique index, so this
-- enforces "at most one settled row per run" while leaving the refuse audit
-- trail unconstrained — Story 2.2 requires one row per refused attempt, so a
-- plain UNIQUE(run_id) would destroy it.
--
-- Statement order is load-bearing: ADD COLUMN -> backfill -> CREATE UNIQUE
-- INDEX. Creating the index first fails on any pre-existing duplicate.

-- AlterTable
ALTER TABLE "attested_settlements" ADD COLUMN     "settled_run_id" TEXT;

-- Backfill — RULED EXPLICITLY (Story 2.3): pre-existing settled rows ARE
-- claimed. Leaving them NULL would create a boundary date before which a run
-- could be settled a second time and the index would not catch it.
--
-- If a run somehow already holds several settled rows, the EARLIEST keeps the
-- claim and the others stay NULL; without this the index below cannot build.
UPDATE "attested_settlements" a
SET "settled_run_id" = a."run_id"
WHERE a."outcome" = 'settled'
  AND a."id" = (
    SELECT a2."id"
    FROM "attested_settlements" a2
    WHERE a2."run_id" = a."run_id"
      AND a2."outcome" = 'settled'
    ORDER BY a2."created_at" ASC, a2."id" ASC
    LIMIT 1
  );

-- CreateIndex
CREATE UNIQUE INDEX "attested_settlements_settled_run_id_key" ON "attested_settlements"("settled_run_id");
