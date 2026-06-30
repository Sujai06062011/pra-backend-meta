-- Migration 009: Add daily_summary scheduler config for all active doctors
-- Safe to re-run: ON CONFLICT DO NOTHING

INSERT INTO clinic_config (doctor_id, config_key, config_value, config_type, description)
SELECT
    id AS doctor_id,
    'scheduler.daily_summary.time' AS config_key,
    '08:00' AS config_value,
    'time' AS config_type,
    'Time to send daily morning summary to doctor WhatsApp (HH:MM, IST)' AS description
FROM doctors
WHERE is_available = true
ON CONFLICT DO NOTHING;

-- Enable the feature flag for all active doctors
INSERT INTO clinic_config (doctor_id, config_key, config_value, config_type, description)
SELECT
    id AS doctor_id,
    'feature.daily_summary.enabled' AS config_key,
    'true' AS config_value,
    'boolean' AS config_type,
    'Enable daily morning summary WhatsApp to doctor' AS description
FROM doctors
WHERE is_available = true
ON CONFLICT DO NOTHING;
