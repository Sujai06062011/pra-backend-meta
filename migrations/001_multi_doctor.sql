-- ── Phase 1A: tokens table — add session support ─────────────────────────────
ALTER TABLE tokens
  ADD COLUMN IF NOT EXISTS session VARCHAR(10) DEFAULT 'morning',
  ADD COLUMN IF NOT EXISTS token_prefix VARCHAR(2) DEFAULT 'M';

UPDATE tokens SET session = 'morning', token_prefix = 'M'
WHERE session IS NULL;

-- ── Phase 1B: doctors table — add specialty display field ─────────────────────
ALTER TABLE doctors
  ADD COLUMN IF NOT EXISTS specialty_display VARCHAR(100);

UPDATE doctors
SET specialty_display = speciality
WHERE specialty_display IS NULL;

-- ── Phase 1C: Feature flag for Dr. Kumar ──────────────────────────────────────
INSERT INTO clinic_config (doctor_id, config_key, config_value, config_type, description)
VALUES (
  '8c33abe0-5d2e-4613-9437-c7c375e8d162',
  'feature.multi_doctor.enabled',
  'false',
  'boolean',
  'Enable multi-doctor selection in WhatsApp flow'
) ON CONFLICT DO NOTHING;

-- ── Phase 1D: Insert Dr. Poornima ─────────────────────────────────────────────
-- Run this block, then copy the generated id for the schedule/config inserts below.
INSERT INTO doctors (
  name, speciality, specialty_display, qualification,
  mobile, email, clinic_name, is_available,
  whatsapp_number, online_consultation_enabled, online_consultation_fee
) VALUES (
  'Dr. Poornima',
  'Gynaecologist',
  'Gynaecology',
  'MBBS, MD (OBG)',
  '9943941314',
  'drpoornima@praclinic.com',
  'Dr. Kumar Child Care Clinic',
  true,
  '918438055569',
  false,
  0
);

-- After running the above, get Dr. Poornima's id:
-- SELECT id FROM doctors WHERE email = 'drpoornima@praclinic.com';
-- Then run the schedule and config inserts with that id.
-- Replace {POORNIMA_ID} below with the actual UUID.

-- ── Doctor schedule for Dr. Poornima (7 days) ─────────────────────────────────
-- (Uncomment and replace {POORNIMA_ID} after you have her UUID)
/*
INSERT INTO clinic_availability (doctor_id, availability_date, is_holiday,
  morning_enabled, morning_start, morning_end,
  evening_enabled, evening_start, evening_end)
SELECT
  '{POORNIMA_ID}',
  generate_series::date,
  false,
  true, '10:00', '13:30',
  true, '17:00', '20:00'
FROM generate_series(
  CURRENT_DATE,
  CURRENT_DATE + INTERVAL '365 days',
  INTERVAL '1 day'
) gs(generate_series)
WHERE EXTRACT(DOW FROM generate_series::date) NOT IN (0);  -- exclude Sundays
*/

-- ── Feature flag for Dr. Poornima ─────────────────────────────────────────────
-- (Uncomment and replace {POORNIMA_ID} after you have her UUID)
/*
INSERT INTO clinic_config (doctor_id, config_key, config_value, config_type, description)
VALUES
  ('{POORNIMA_ID}', 'feature.multi_doctor.enabled', 'false', 'boolean',
   'Enable multi-doctor selection in WhatsApp flow'),
  ('{POORNIMA_ID}', 'clinic.slot_start_morning',    '10:00', 'string',  'Morning session start'),
  ('{POORNIMA_ID}', 'clinic.slot_end_morning',      '13:30', 'string',  'Morning session end'),
  ('{POORNIMA_ID}', 'clinic.slot_start_evening',    '17:00', 'string',  'Evening session start'),
  ('{POORNIMA_ID}', 'clinic.slot_end_evening',      '20:00', 'string',  'Evening session end'),
  ('{POORNIMA_ID}', 'clinic.slot_duration_minutes', '15',    'integer', 'Slot duration in minutes')
ON CONFLICT DO NOTHING;
*/

-- ── Rollback instructions ──────────────────────────────────────────────────────
-- To disable multi-doctor without rolling back schema:
--   UPDATE clinic_config SET config_value = 'false'
--   WHERE config_key = 'feature.multi_doctor.enabled';
--
-- To fully roll back schema additions:
--   ALTER TABLE tokens DROP COLUMN IF EXISTS session, DROP COLUMN IF EXISTS token_prefix;
--   ALTER TABLE doctors DROP COLUMN IF EXISTS specialty_display;
--   DELETE FROM doctors WHERE email = 'drpoornima@praclinic.com';
--   DELETE FROM clinic_config WHERE config_key = 'feature.multi_doctor.enabled';
