-- Rebrand: TrueCare Family Clinic, Dr. Deepa (Gynaecologist)
-- Dr. Kumar stays, clinic name changes everywhere
-- Dr. Poornima → Dr. Deepa with Gynaecologist specialty

-- Update clinic name for both doctors
UPDATE doctors
SET clinic_name = 'TrueCare Family Clinic'
WHERE whatsapp_number = '918438055569';

-- Update Dr. Poornima → Dr. Deepa (Gynaecologist)
UPDATE doctors
SET
  name            = 'Dr. Deepa',
  speciality      = 'Gynaecologist',
  specialty_display = 'Gynaecologist'
WHERE id = '98f2b186-bee6-4849-adff-a8b9176d0d22';

-- Update Dr. Kumar specialty display (keep Paediatrics, fix display)
UPDATE doctors
SET specialty_display = 'Paediatrician'
WHERE id = '8c33abe0-5d2e-4613-9437-c7c375e8d162';

-- Update clinic_staff: drpoornima → drdeepaa
UPDATE clinic_staff
SET name = 'Dr. Deepa', username = 'drdeepaa'
WHERE username = 'drpoornima';

-- Update clinic_staff: drkumar display name if needed
UPDATE clinic_staff
SET name = 'Dr. Kumar'
WHERE username = 'drkumar';
