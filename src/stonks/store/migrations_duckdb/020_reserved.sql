-- Reserved (BE-63): no schema change. Number 020 is held for a migration
-- another work package writes in parallel (roadmap Phase 20). This no-op keeps
-- the numbers contiguous (tests/unit/test_migration_numbers.py) and gives way
-- to that migration when the branches merge.
SELECT 1;
