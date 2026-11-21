-- A reference acquired after GC claims a file would otherwise race the unlink.
-- Lock the object row with FOR SHARE: this conflicts with the GC state update
-- (which takes a NO KEY UPDATE lock) until the transaction commits.
CREATE OR REPLACE FUNCTION vr2_enforce_live_object_ref() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1
      FROM vr2_objects
     WHERE storage_key = NEW.storage_key
       AND state = 'live'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'object is not live; reference denied'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER vr2_object_refs_live_fence
BEFORE INSERT OR UPDATE OF storage_key ON vr2_object_refs
FOR EACH ROW EXECUTE FUNCTION vr2_enforce_live_object_ref();
