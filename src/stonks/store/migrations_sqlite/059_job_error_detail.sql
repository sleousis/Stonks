-- The raw error of a job that crashed, kept for admins.
--
-- A job that stops on an unexpected exception stores a plain sentence in
-- `error`, which everyone who may see the job reads. The exception text
-- ("TypeError: cannot pickle ...") goes here, and the API returns it to
-- admins only. NULL for jobs that succeeded or failed with a message
-- written for people.

ALTER TABLE jobs ADD COLUMN error_detail TEXT;
