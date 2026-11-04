-- Story 2.0: offices are the tenant boundary (PRD FR-32/33/36).
-- Additive: two tables, one enum, three nullable columns, then a backfill that
-- gives every existing user a personal office (id 'po_' || user id, the same id
-- the Gateway creates on first use) and assigns their routines, connections and
-- sessions to it. No receipt, attestation or settlement row is touched.

-- CreateEnum
CREATE TYPE "OfficeRole" AS ENUM ('OWNER', 'MEMBER');

-- CreateTable
CREATE TABLE "offices" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "personal_owner_id" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "offices_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "office_members" (
    "id" TEXT NOT NULL,
    "office_id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "role" "OfficeRole" NOT NULL DEFAULT 'MEMBER',
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "office_members_pkey" PRIMARY KEY ("id")
);

-- AlterTable
ALTER TABLE "agents" ADD COLUMN "office_id" TEXT;
ALTER TABLE "workflow_templates" ADD COLUMN "office_id" TEXT;
ALTER TABLE "conversation_sessions" ADD COLUMN "office_id" TEXT;

-- CreateIndex
CREATE UNIQUE INDEX "offices_personal_owner_id_key" ON "offices"("personal_owner_id");
CREATE INDEX "office_members_user_id_idx" ON "office_members"("user_id");
CREATE UNIQUE INDEX "office_members_office_id_user_id_key" ON "office_members"("office_id", "user_id");
CREATE INDEX "agents_office_id_idx" ON "agents"("office_id");
CREATE INDEX "workflow_templates_office_id_user_id_idx" ON "workflow_templates"("office_id", "user_id");
CREATE INDEX "conversation_sessions_office_id_user_id_idx" ON "conversation_sessions"("office_id", "user_id");

-- AddForeignKey
ALTER TABLE "offices" ADD CONSTRAINT "offices_personal_owner_id_fkey" FOREIGN KEY ("personal_owner_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "office_members" ADD CONSTRAINT "office_members_office_id_fkey" FOREIGN KEY ("office_id") REFERENCES "offices"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "office_members" ADD CONSTRAINT "office_members_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "agents" ADD CONSTRAINT "agents_office_id_fkey" FOREIGN KEY ("office_id") REFERENCES "offices"("id") ON DELETE SET NULL ON UPDATE CASCADE;
ALTER TABLE "workflow_templates" ADD CONSTRAINT "workflow_templates_office_id_fkey" FOREIGN KEY ("office_id") REFERENCES "offices"("id") ON DELETE SET NULL ON UPDATE CASCADE;
ALTER TABLE "conversation_sessions" ADD CONSTRAINT "conversation_sessions_office_id_fkey" FOREIGN KEY ("office_id") REFERENCES "offices"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- Backfill: one personal office per existing user, owned by that user.
INSERT INTO "offices" ("id", "name", "personal_owner_id", "updated_at")
SELECT 'po_' || "id", 'Personal', "id", CURRENT_TIMESTAMP FROM "users";

INSERT INTO "office_members" ("id", "office_id", "user_id", "role")
SELECT 'pm_' || "id", 'po_' || "id", "id", 'OWNER' FROM "users";

UPDATE "workflow_templates" SET "office_id" = 'po_' || "user_id" WHERE "office_id" IS NULL;
UPDATE "conversation_sessions" SET "office_id" = 'po_' || "user_id" WHERE "office_id" IS NULL;
-- Only connections belong to an office; catalogue and platform agents stay NULL.
UPDATE "agents" SET "office_id" = 'po_' || "user_id"
WHERE "office_id" IS NULL AND 'connection' = ANY("tags");
