-- CreateTable
CREATE TABLE "attested_settlements" (
    "id" TEXT NOT NULL,
    "run_id" TEXT NOT NULL,
    "session_id" TEXT,
    "outcome" TEXT NOT NULL,
    "envelope_digest" TEXT NOT NULL,
    "failed_checks" JSONB NOT NULL,
    "charter_hash" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "attested_settlements_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "attested_settlements_run_id_idx" ON "attested_settlements"("run_id");
