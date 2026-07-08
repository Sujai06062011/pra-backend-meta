-- ============================================================
-- Lab Reports Migration
-- Run in Supabase SQL Editor
-- ============================================================

-- ── 1. lab_orders (must exist before lab_reports FK) ────────
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

-- ── 2. lab_reports (fresh CREATE — table did not exist) ─────
CREATE TABLE IF NOT EXISTS lab_reports (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  patient_id               UUID REFERENCES patients(id) ON DELETE CASCADE,
  doctor_id                UUID REFERENCES doctors(id),
  order_id                 UUID REFERENCES lab_orders(id) NULL,
  report_name              VARCHAR(200),
  test_name                VARCHAR(200),
  test_category            VARCHAR(50),
  lab_name                 VARCHAR(100),
  report_date              DATE,
  received_date            DATE DEFAULT CURRENT_DATE,
  report_source            VARCHAR(30) DEFAULT 'dashboard_upload',
  pdf_url                  TEXT,
  image_url                TEXT,
  status                   VARCHAR(30) DEFAULT 'Pending Review',
  result_summary           TEXT,
  ocr_raw_text             TEXT,
  extracted_values         JSONB,
  doctor_notes             TEXT,
  whatsapp_sent_to_patient BOOLEAN DEFAULT FALSE,
  external_order_id        VARCHAR(100),
  lab_source               VARCHAR(50),
  raw_api_payload          JSONB,
  created_at               TIMESTAMPTZ DEFAULT NOW(),
  updated_at               TIMESTAMPTZ DEFAULT NOW()
);

-- ── 3. lab_report_values ────────────────────────────────────
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

-- ── 5. Storage bucket ────────────────────────────────────────
-- Run this separately if the bucket doesn't exist yet:
-- INSERT INTO storage.buckets (id, name, public)
-- VALUES ('lab-reports', 'lab-reports', true)
-- ON CONFLICT DO NOTHING;
