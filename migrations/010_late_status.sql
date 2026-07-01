-- Add returned_at column for tracking when Late patients return to queue
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS returned_at TIMESTAMPTZ;
