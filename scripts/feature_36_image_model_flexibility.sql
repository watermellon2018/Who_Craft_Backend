-- Feature 36: deploy the image-model snapshot schema to an existing database.
--
-- The repository's w_craft_back/migrations directory is currently not a
-- Python package, so Django does not discover its migration files. Keep this
-- script as the explicit production upgrade path until that legacy migration
-- baseline is repaired. The statements are safe to run more than once.

BEGIN;

ALTER TABLE character_generation_jobs
    ADD COLUMN IF NOT EXISTS provider_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE character_generation_jobs
    ALTER COLUMN provider TYPE varchar(255),
    ALTER COLUMN model_name TYPE varchar(255),
    ALTER COLUMN model_version TYPE varchar(255);

ALTER TABLE user_profiles
    ALTER COLUMN image_generation_model TYPE varchar(255);

COMMIT;
