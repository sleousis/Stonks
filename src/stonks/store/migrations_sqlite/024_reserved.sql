-- Reserved (BE-63): no schema change. Number 024 was skipped when 025 landed.
-- This no-op fills the gap so a later migration can never take 024 and run
-- after 025 on an existing install. Migration numbers stay contiguous
-- (tests/unit/test_migration_numbers.py).
SELECT 1;
