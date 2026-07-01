-- Add returned_at column for tracking when Late patients return to queue
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS returned_at TIMESTAMPTZ;

-- Extend status check constraint to allow "Late"
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_status_check;
ALTER TABLE appointments ADD CONSTRAINT appointments_status_check
  CHECK (status IN ('Confirmed', 'In Progress', 'Completed', 'Cancelled', 'No-Show', 'Late'));
