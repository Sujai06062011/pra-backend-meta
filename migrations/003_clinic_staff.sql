-- clinic_staff: username+PIN auth, role-based access
-- PIN hash is SHA-256 of the PIN string (set via staff management screen or reset below)
-- Default PIN for all seeded users: 1234
-- SHA-256("1234") = 03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4

CREATE TABLE IF NOT EXISTS clinic_staff (
  id              UUID    DEFAULT gen_random_uuid() PRIMARY KEY,
  clinic_whatsapp TEXT    NOT NULL,                -- identifies the clinic e.g. '918438055569'
  doctor_id       UUID    REFERENCES doctors(id),  -- set only for role='doctor'
  role            TEXT    NOT NULL CHECK (role IN ('doctor','receptionist','pharmacist','lab','admin')),
  name            TEXT    NOT NULL,
  username        TEXT    UNIQUE NOT NULL,
  pin_hash        TEXT    NOT NULL,
  is_active       BOOLEAN DEFAULT true,
  created_at      TIMESTAMPTZ DEFAULT now()
);

-- Seed initial staff for Dr. Kumar Child Care clinic
-- All PINs default to 1234 — change immediately via /staff screen

INSERT INTO clinic_staff (clinic_whatsapp, doctor_id, role, name, username, pin_hash) VALUES
  -- Admin (receptionist who can manage staff)
  ('918438055569', NULL,
   'admin', 'Admin', 'admin',
   '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4'),

  -- Dr. Kumar
  ('918438055569', '8c33abe0-5d2e-4613-9437-c7c375e8d162',
   'doctor', 'Dr. Kumar', 'drkumar',
   '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4'),

  -- Dr. Poornima
  ('918438055569', '98f2b186-bee6-4849-adff-a8b9176d0d22',
   'doctor', 'Dr. Poornima', 'drpoornima',
   '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4'),

  -- Receptionist
  ('918438055569', NULL,
   'receptionist', 'Reception', 'reception',
   '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4')

ON CONFLICT (username) DO NOTHING;
