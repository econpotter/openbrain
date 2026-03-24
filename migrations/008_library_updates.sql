-- Rename journal → source (more generic across doc types)
ALTER TABLE library RENAME COLUMN journal TO source;

-- Drop doc_type CHECK constraint so any string is valid
ALTER TABLE library DROP CONSTRAINT IF EXISTS library_doc_type_check;

-- Add doi and bibtex columns
ALTER TABLE library ADD COLUMN IF NOT EXISTS doi TEXT;
ALTER TABLE library ADD COLUMN IF NOT EXISTS bibtex TEXT;
