-- One PostgreSQL instance, two logically separate databases (see "Данные" in the design doc).
-- Each service has its own role and can only reach its own database.
-- Dev credentials only: change them (and the DATABASE_URL values) outside of a demo.

CREATE ROLE courses LOGIN PASSWORD 'courses';
CREATE DATABASE courses_db OWNER courses;

CREATE ROLE submissions LOGIN PASSWORD 'submissions';
CREATE DATABASE submissions_db OWNER submissions;

REVOKE ALL ON DATABASE courses_db FROM PUBLIC;
REVOKE ALL ON DATABASE submissions_db FROM PUBLIC;
GRANT ALL ON DATABASE courses_db TO courses;
GRANT ALL ON DATABASE submissions_db TO submissions;
