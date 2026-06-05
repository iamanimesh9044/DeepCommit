-- Recreate common traversal views for trials.sqlite

-- Experiments basic slice
DROP VIEW IF EXISTS v_experiments_basic;
CREATE VIEW v_experiments_basic AS
SELECT
  trial,
  repo_id,
  agents,
  model,
  status,
  status_str,
  validation_error,
  agent_cli_status,
  agent_cli_exit_code,
  agent_cli_num_turns,
  end_ts,
  duration_sec,
  milestones_count,
  dependencies_count
FROM experiments;

-- Latest experiment per repo (by end_ts)
DROP VIEW IF EXISTS v_experiments_latest_per_repo;
CREATE VIEW v_experiments_latest_per_repo AS
SELECT e.*
FROM experiments e
WHERE e.end_ts = (
  SELECT MAX(end_ts) FROM experiments WHERE repo_id = e.repo_id
);

-- Flatten metrics JSON to key-value rows
DROP VIEW IF EXISTS v_metrics_kv;
CREATE VIEW v_metrics_kv AS
SELECT e.trial, j.key AS key, j.value AS value
FROM experiments e, json_each(e.metrics_json) AS j;

-- Milestones with titles
DROP VIEW IF EXISTS v_milestones_full;
CREATE VIEW v_milestones_full AS
SELECT m.trial, m.milestone_id, m.title
FROM milestones m;

-- Dependencies with titles resolved
DROP VIEW IF EXISTS v_milestone_edges;
CREATE VIEW v_milestone_edges AS
SELECT d.trial,
       d.prerequisite_id,
       s.title AS source_title,
       d.dependant_id,
       t.title AS target_title
FROM dependencies d
LEFT JOIN milestones s ON s.trial = d.trial AND s.milestone_id = d.prerequisite_id
LEFT JOIN milestones t ON t.trial = d.trial AND t.milestone_id = d.dependant_id;

-- Milestone membership with milestone title and repo
DROP VIEW IF EXISTS v_milestone_commits_full;
CREATE VIEW v_milestone_commits_full AS
SELECT mc.trial,
       mc.repo_id,
       mc.milestone_id,
       m.title AS milestone_title,
       mc.commit_sha
FROM milestone_commits mc
LEFT JOIN milestones m ON m.trial = mc.trial AND m.milestone_id = mc.milestone_id;

-- Commit co-occurrence pairs within the same milestone/trial
DROP VIEW IF EXISTS v_commit_pairs;
CREATE VIEW v_commit_pairs AS
SELECT a.trial,
       a.milestone_id,
       a.commit_sha AS commit_a,
       b.commit_sha AS commit_b
FROM milestone_commits a
JOIN milestone_commits b
  ON a.trial = b.trial
 AND a.milestone_id = b.milestone_id
 AND a.commit_sha < b.commit_sha;

-- Quick filter for failures/validation issues
DROP VIEW IF EXISTS v_experiments_errors;
CREATE VIEW v_experiments_errors AS
SELECT trial,
       status,
       status_str,
       validation_error,
       agent_cli_status,
       agent_cli_exit_code,
       agent_cli_stderr,
       end_ts
FROM experiments
WHERE status <> 'ok'
   OR IFNULL(agent_cli_exit_code, 0) <> 0
   OR IFNULL(status_str, '') <> ''
   OR IFNULL(validation_error, '') <> '';

