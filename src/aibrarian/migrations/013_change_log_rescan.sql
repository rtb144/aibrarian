-- books rescan: re-cataloguing a suggestion in place is logged as change_log op 'rescan'
-- (details: old/new status, score, decision).
ALTER TABLE aib.change_log DROP CONSTRAINT IF EXISTS change_log_op_check;
ALTER TABLE aib.change_log ADD CONSTRAINT change_log_op_check
    CHECK (op IN ('move','rename','quarantine','restore','delete_row','insert','update_path',
                  'relink','rescan'));
