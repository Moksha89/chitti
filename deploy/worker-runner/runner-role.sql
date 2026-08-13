-- The deployment script creates this login with a generated password in a
-- pipe, then derives and applies table grants from the runner import graph.
GRANT CONNECT ON DATABASE chitti TO chitti_runner;
GRANT USAGE ON SCHEMA public TO chitti_runner;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM chitti_runner;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM chitti_runner;
