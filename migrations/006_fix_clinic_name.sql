-- Fix clinic.name for Dr. Kumar to TrueCare Family Clinic
UPDATE clinic_config
SET config_value = 'TrueCare Family Clinic'
WHERE config_key = 'clinic.name'
  AND doctor_id = '8c33abe0-5d2e-4613-9437-c7c375e8d162';
