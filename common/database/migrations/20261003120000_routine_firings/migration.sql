-- Story 2.2 (AD-19, AD-22): a firing is a row before it is a run.
-- Additive: a new enum and a new table; no existing row changes.

-- CreateEnum
CREATE TYPE "FiringState" AS ENUM ('scheduled', 'running', 'attested_unsettled', 'settled', 'refused', 'paused', 'skipped', 'error');

-- CreateTable
CREATE TABLE "routine_firings" (
    "id" TEXT NOT NULL,
    "routine_id" TEXT NOT NULL,
    "office_id" TEXT,
    "user_id" TEXT NOT NULL,
    "slot" TIMESTAMP(3) NOT NULL,
    "state" "FiringState" NOT NULL DEFAULT 'scheduled',
    "detail" TEXT,
    "session_id" TEXT,
    "run_id" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "routine_firings_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "routine_firings_session_id_key" ON "routine_firings"("session_id");
CREATE INDEX "routine_firings_routine_id_state_idx" ON "routine_firings"("routine_id", "state");
CREATE INDEX "routine_firings_office_id_idx" ON "routine_firings"("office_id");
CREATE UNIQUE INDEX "routine_firings_routine_id_slot_key" ON "routine_firings"("routine_id", "slot");

-- AddForeignKey
ALTER TABLE "routine_firings" ADD CONSTRAINT "routine_firings_routine_id_fkey" FOREIGN KEY ("routine_id") REFERENCES "workflow_templates"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "routine_firings" ADD CONSTRAINT "routine_firings_office_id_fkey" FOREIGN KEY ("office_id") REFERENCES "offices"("id") ON DELETE SET NULL ON UPDATE CASCADE;
ALTER TABLE "routine_firings" ADD CONSTRAINT "routine_firings_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;
