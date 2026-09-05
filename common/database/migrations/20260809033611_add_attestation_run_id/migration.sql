-- AlterTable
ALTER TABLE "attestations" ADD COLUMN     "run_id" TEXT;

-- CreateIndex
CREATE UNIQUE INDEX "attestations_run_id_key" ON "attestations"("run_id");
