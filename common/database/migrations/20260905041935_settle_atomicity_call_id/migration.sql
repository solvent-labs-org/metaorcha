-- Story 2.4 — settled-audit and credit write in one transaction (AR-11).
--
-- `call_id` records the charged call an outcome settles or refuses, so an
-- audit row can be matched to its `transactions` row exactly
-- (`transactions.call_id` is unique) rather than approximately by session.
-- Nullable and additive: rows written before this column, and gate calls that
-- carry no call, leave it NULL. Nothing about the `settled_run_id` claim or
-- its unique index changes.

-- AlterTable
ALTER TABLE "attested_settlements" ADD COLUMN     "call_id" TEXT;

-- CreateIndex
CREATE INDEX "attested_settlements_call_id_idx" ON "attested_settlements"("call_id");
