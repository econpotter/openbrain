-- Remove reading_list from library status options
ALTER TABLE library DROP CONSTRAINT IF EXISTS library_status_check;
ALTER TABLE library ADD CONSTRAINT library_status_check
    CHECK (status IN ('ingested', 'reading', 'read', 'archived'));
