-- Run in Supabase SQL Editor
-- Inserts per-day schedule config keys into clinic_config.
-- ON CONFLICT DO NOTHING — safe to run multiple times.
-- evening_end uses 21:30 (matching clinic.slot_end_evening) — prevents false boundary errors.

DO $$
DECLARE
  d_id  UUID    := '8c33abe0-5d2e-4613-9437-c7c375e8d162';
  days  TEXT[]  := ARRAY['monday','tuesday','wednesday','thursday','friday','saturday','sunday'];
  day   TEXT;
  open  TEXT;
BEGIN
  FOREACH day IN ARRAY days LOOP
    open := CASE WHEN day = 'sunday' THEN 'false' ELSE 'true' END;

    INSERT INTO clinic_config (doctor_id, config_key, config_value, config_type, description)
    VALUES
      (d_id, 'clinic.schedule.' || day || '.enabled',               open,    'boolean', initcap(day) || ' — clinic open'),
      (d_id, 'clinic.schedule.' || day || '.slot_duration_minutes', '10',    'integer', initcap(day) || ' slot duration (min)'),
      (d_id, 'clinic.schedule.' || day || '.morning_enabled',       open,    'boolean', initcap(day) || ' — morning session'),
      (d_id, 'clinic.schedule.' || day || '.morning_start',         '09:30', 'time',    initcap(day) || ' morning start'),
      (d_id, 'clinic.schedule.' || day || '.morning_end',           '13:30', 'time',    initcap(day) || ' morning end'),
      (d_id, 'clinic.schedule.' || day || '.evening_enabled',       open,    'boolean', initcap(day) || ' — evening session'),
      (d_id, 'clinic.schedule.' || day || '.evening_start',         '17:30', 'time',    initcap(day) || ' evening start'),
      (d_id, 'clinic.schedule.' || day || '.evening_end',           '21:30', 'time',    initcap(day) || ' evening end')
    ON CONFLICT (doctor_id, config_key) DO NOTHING;
  END LOOP;
END $$;

-- Seed slot_duration_minutes for any existing rows that predate this column
INSERT INTO clinic_config (doctor_id, config_key, config_value, config_type, description)
SELECT '8c33abe0-5d2e-4613-9437-c7c375e8d162',
       'clinic.schedule.' || d || '.slot_duration_minutes',
       '10', 'integer', initcap(d) || ' slot duration (min)'
FROM unnest(ARRAY['monday','tuesday','wednesday','thursday','friday','saturday','sunday']) AS d
ON CONFLICT (doctor_id, config_key) DO NOTHING;
