-- ============================================================
-- Lab Reports Migration
-- Run in Supabase SQL Editor
-- lab_reports table already exists — use ALTER TABLE only
-- ============================================================

-- ── 1. Extend existing lab_reports table ────────────────────
ALTER TABLE lab_reports
  ADD COLUMN IF NOT EXISTS order_id UUID REFERENCES lab_orders(id) NULL,
  ADD COLUMN IF NOT EXISTS test_name VARCHAR(200),
  ADD COLUMN IF NOT EXISTS test_category VARCHAR(50),
  ADD COLUMN IF NOT EXISTS lab_name VARCHAR(100),
  ADD COLUMN IF NOT EXISTS report_date DATE,
  ADD COLUMN IF NOT EXISTS received_date DATE DEFAULT CURRENT_DATE,
  ADD COLUMN IF NOT EXISTS report_source VARCHAR(30) DEFAULT 'dashboard_upload',
  ADD COLUMN IF NOT EXISTS pdf_url TEXT,
  ADD COLUMN IF NOT EXISTS image_url TEXT,
  ADD COLUMN IF NOT EXISTS result_summary TEXT,
  ADD COLUMN IF NOT EXISTS ocr_raw_text TEXT,
  ADD COLUMN IF NOT EXISTS extracted_values JSONB,
  ADD COLUMN IF NOT EXISTS doctor_notes TEXT,
  ADD COLUMN IF NOT EXISTS whatsapp_sent_to_patient BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS external_order_id VARCHAR(100),
  ADD COLUMN IF NOT EXISTS lab_source VARCHAR(50),
  ADD COLUMN IF NOT EXISTS raw_api_payload JSONB,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- Migrate existing status values to new enum vocabulary
UPDATE lab_reports SET status = 'Pending Review' WHERE status = 'pending';
UPDATE lab_reports SET status = 'Reviewed'       WHERE status = 'reviewed';

-- ── 2. lab_orders (new) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS lab_orders (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  patient_id       UUID REFERENCES patients(id) ON DELETE CASCADE,
  doctor_id        UUID REFERENCES doctors(id),
  prescription_id  UUID REFERENCES prescriptions(id) NULL,
  test_name        VARCHAR(200) NOT NULL,
  test_category    VARCHAR(50),
  priority         VARCHAR(20) DEFAULT 'Routine',
  lab_type         VARCHAR(20) DEFAULT 'external',
  lab_name         VARCHAR(100),
  status           VARCHAR(30) DEFAULT 'Ordered',
  notes            TEXT,
  ordered_at       TIMESTAMPTZ DEFAULT NOW(),
  collected_at     TIMESTAMPTZ,
  processing_at    TIMESTAMPTZ,
  ready_at         TIMESTAMPTZ,
  delivered_at     TIMESTAMPTZ,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── 3. lab_report_values (new) ──────────────────────────────
CREATE TABLE IF NOT EXISTS lab_report_values (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  report_id          UUID REFERENCES lab_reports(id) ON DELETE CASCADE,
  patient_id         UUID REFERENCES patients(id),
  parameter_name     VARCHAR(100) NOT NULL,
  parameter_category VARCHAR(50),
  value              DECIMAL(12,4) NOT NULL,
  unit               VARCHAR(30),
  ref_low            DECIMAL(12,4),
  ref_high           DECIMAL(12,4),
  status             VARCHAR(20) DEFAULT 'Normal',
  report_date        DATE NOT NULL,
  created_at         TIMESTAMPTZ DEFAULT NOW()
);

-- ── 4. Indexes ───────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_lab_values_patient_param
  ON lab_report_values(patient_id, parameter_name, report_date);
CREATE INDEX IF NOT EXISTS idx_lab_reports_patient
  ON lab_reports(patient_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lab_reports_doctor_status
  ON lab_reports(doctor_id, status);
CREATE INDEX IF NOT EXISTS idx_lab_orders_doctor
  ON lab_orders(doctor_id, status);
CREATE INDEX IF NOT EXISTS idx_lab_orders_patient
  ON lab_orders(patient_id);

-- ── 5. Storage bucket (run separately if needed) ─────────────
-- INSERT INTO storage.buckets (id, name, public)
-- VALUES ('lab-reports', 'lab-reports', true)
-- ON CONFLICT DO NOTHING;
