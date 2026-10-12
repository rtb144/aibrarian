-- Calibre ingest, audiobook copy and Calibre validation actions.
ALTER TABLE aib.library_action DROP CONSTRAINT IF EXISTS library_action_action_check;
ALTER TABLE aib.library_action ADD CONSTRAINT library_action_action_check
    CHECK (action IN ('rename','move','copy','quarantine','skip','needs_review','error','undo',
                      'folder_removed','calibre_add','add_format','validate'));
