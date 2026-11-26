-- config_change gets the same decision fields as knowledge (owner approval flow).
ALTER TABLE config_change ADD COLUMN decided_by TEXT;
