```sql
-- =========================================================
-- PROD-ready schema: identity by account login (IIN), not UUID
-- =========================================================

-- 1) Base tables (greenfield)
CREATE TABLE IF NOT EXISTS public.profiles (
  id UUID REFERENCES auth.users NOT NULL PRIMARY KEY,
  login TEXT UNIQUE NOT NULL,
  full_name TEXT,
  role TEXT DEFAULT 'student' CHECK (role IN ('student', 'teacher')),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.activity_logs (
  id BIGSERIAL PRIMARY KEY,
  student_login TEXT NOT NULL REFERENCES public.profiles(login) ON UPDATE CASCADE ON DELETE CASCADE,
  active_window TEXT,
  process_list JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_activity_logs_student_login_created_at
ON public.activity_logs(student_login, created_at DESC);

-- Optional strict check for school IIN logins (12 digits).
-- Enable only if all student logins are guaranteed IIN format.
-- ALTER TABLE public.activity_logs
--   ADD CONSTRAINT activity_logs_student_login_iin_chk
--   CHECK (student_login ~ '^[0-9]{12}$');
```

```sql
-- =========================================================
-- Zero-downtime migration: existing project with student_id UUID
-- =========================================================

BEGIN;

-- A) Add new login column
ALTER TABLE public.activity_logs
  ADD COLUMN IF NOT EXISTS student_login TEXT;

-- B) Backfill login from profiles by old FK
UPDATE public.activity_logs al
SET student_login = p.login
FROM public.profiles p
WHERE al.student_id = p.id
  AND al.student_login IS NULL;

-- C) Enforce new FK + NOT NULL
ALTER TABLE public.activity_logs
  ALTER COLUMN student_login SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'activity_logs_student_login_fkey'
  ) THEN
    ALTER TABLE public.activity_logs
      ADD CONSTRAINT activity_logs_student_login_fkey
      FOREIGN KEY (student_login)
      REFERENCES public.profiles(login)
      ON UPDATE CASCADE
      ON DELETE CASCADE;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_activity_logs_student_login_created_at
ON public.activity_logs(student_login, created_at DESC);

COMMIT;
```

```sql
-- =========================================================
-- RLS policies (login-based)
-- =========================================================

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.activity_logs ENABLE ROW LEVEL SECURITY;

-- Remove legacy INSERT policy by UUID (if it exists)
DROP POLICY IF EXISTS "Students can insert own logs" ON public.activity_logs;

-- Student can read only own profile
DROP POLICY IF EXISTS "Students can view own profile" ON public.profiles;
CREATE POLICY "Students can view own profile"
ON public.profiles FOR SELECT
USING (auth.uid() = id);

-- Teacher can read all logs
DROP POLICY IF EXISTS "Teachers can view all logs" ON public.activity_logs;
CREATE POLICY "Teachers can view all logs"
ON public.activity_logs FOR SELECT
USING (
  EXISTS (
    SELECT 1
    FROM public.profiles p
    WHERE p.id = auth.uid()
      AND p.role = 'teacher'
  )
);

-- Student can insert only logs bound to own login
CREATE POLICY "Students can insert own logs by login"
ON public.activity_logs FOR INSERT
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.profiles p
    WHERE p.id = auth.uid()
      AND p.login = student_login
  )
);
```

```sql
-- =========================================================
-- Final cleanup (execute only after all clients switched)
-- =========================================================

-- If old UUID column still exists and no longer used:
-- ALTER TABLE public.activity_logs DROP COLUMN IF EXISTS student_id;
```
