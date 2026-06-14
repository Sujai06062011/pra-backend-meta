-- Run this in Supabase SQL Editor (project: gvagwplrmurbvwjnvckf)
-- Dashboard → SQL Editor → New Query → paste → Run

CREATE TABLE IF NOT EXISTS clinic_schedule (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  doctor_id        UUID NOT NULL,
  day_of_week      TEXT NOT NULL,  -- 'monday', 'tuesday', ..., 'sunday'

  is_closed        BOOLEAN DEFAULT FALSE,  -- TRUE = clinic closed this weekday

  morning_enabled  BOOLEAN DEFAULT TRUE,
  morning_start    TIME,  -- NULL = use clinic_config default
  morning_end      TIME,

  evening_enabled  BOOLEAN DEFAULT TRUE,
  evening_start    TIME,
  evening_end      TIME,

  created_at       TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW(),

  UNIQUE(doctor_id, day_of_week),
  CHECK (day_of_week IN ('monday','tuesday','wednesday','thursday','friday','saturday','sunday'))
);

CREATE INDEX IF NOT EXISTS idx_clinic_schedule_doctor
  ON clinic_schedule(doctor_id);

-- Permissions
GRANT SELECT, INSERT, UPDATE, DELETE ON public.clinic_schedule TO service_role, anon, authenticated;
