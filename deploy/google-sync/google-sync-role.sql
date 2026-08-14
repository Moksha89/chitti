-- The deployment script creates this login with a generated password.
GRANT CONNECT ON DATABASE chitti TO chitti_google_sync;
GRANT USAGE ON SCHEMA public TO chitti_google_sync;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM chitti_google_sync;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM chitti_google_sync;
