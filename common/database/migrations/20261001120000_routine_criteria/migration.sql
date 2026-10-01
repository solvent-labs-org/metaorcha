-- Story 2.1 (AD-19): a routine carries its declared criteria and their operands.
-- Additive with defaults: existing templates read as "no criteria declared".
ALTER TABLE "workflow_templates" ADD COLUMN "criteria" JSONB NOT NULL DEFAULT '{}';
ALTER TABLE "workflow_templates" ADD COLUMN "criteria_operands" JSONB NOT NULL DEFAULT '{}';
