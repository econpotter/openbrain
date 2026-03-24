-- Add follow-up date to people for CRM-style reminders
ALTER TABLE people ADD COLUMN IF NOT EXISTS follow_up_date DATE;
