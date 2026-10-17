-- Service Desk lab schema.
--
-- Three tables on purpose. This app exists to put realistic, continuously
-- changing data in front of MySQL and to make the tiers talk to each other over
-- the network; it is not trying to be a real ITSM product.
--
-- WHERE THIS RUNS: the MIDDLEWARE host, not the web host. sdapp/api.py is the
-- only module that opens a database connection, so the bootstrap that applies
-- this file (`python -m sdapp.bootstrap`) runs there too. The web host has the
-- file on disk — the payload is identical on both tiers — but no credential to
-- apply it with.
--
-- PORTABILITY: the target is Oracle MySQL 8 on the Ubuntu db host
-- (playbooks/ubuntu-mysql.yml installs mysql-server), but this is kept to
-- syntax MariaDB 10.11 also accepts so the same schema can be tested locally
-- and, if a service is ever repointed, land on a MariaDB host unchanged.
-- That rules out MySQL-8-only spellings; nothing here needs them.

CREATE TABLE IF NOT EXISTS users (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  username    VARCHAR(64)  NOT NULL,
  full_name   VARCHAR(128) NOT NULL,
  email       VARCHAR(190) NOT NULL,
  -- 'agent' can be assigned tickets; 'requester' can only raise them.
  role        ENUM('requester', 'agent') NOT NULL DEFAULT 'requester',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_username (username),
  UNIQUE KEY uq_users_email (email),
  KEY ix_users_role (role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tickets (
  id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
  -- Human-facing reference (SD-001234). The API addresses tickets by this
  -- rather than by id, so generated URLs stay stable and readable in flow logs.
  ref          VARCHAR(16)  NOT NULL,
  subject      VARCHAR(200) NOT NULL,
  body         TEXT         NOT NULL,
  category     VARCHAR(32)  NOT NULL DEFAULT 'general',
  status       ENUM('new', 'open', 'pending', 'resolved', 'closed') NOT NULL DEFAULT 'new',
  priority     ENUM('P1', 'P2', 'P3', 'P4') NOT NULL DEFAULT 'P3',
  requester_id INT UNSIGNED NOT NULL,
  assignee_id  INT UNSIGNED NULL,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  closed_at    DATETIME     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_tickets_ref (ref),
  -- The queue view filters on status and orders by priority then age; this is
  -- the index that keeps it off a full scan as the table grows.
  KEY ix_tickets_status_priority (status, priority, created_at),
  KEY ix_tickets_assignee (assignee_id, status),
  KEY ix_tickets_created (created_at),
  CONSTRAINT fk_tickets_requester FOREIGN KEY (requester_id) REFERENCES users (id),
  CONSTRAINT fk_tickets_assignee  FOREIGN KEY (assignee_id)  REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS comments (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  ticket_id   INT UNSIGNED NOT NULL,
  author_id   INT UNSIGNED NOT NULL,
  body        TEXT         NOT NULL,
  -- Internal notes are agent-only; the requester-facing view filters them out,
  -- which is what makes the detail page do a slightly interesting SELECT.
  is_internal TINYINT(1)   NOT NULL DEFAULT 0,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_comments_ticket (ticket_id, created_at),
  CONSTRAINT fk_comments_ticket FOREIGN KEY (ticket_id) REFERENCES tickets (id) ON DELETE CASCADE,
  CONSTRAINT fk_comments_author FOREIGN KEY (author_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
