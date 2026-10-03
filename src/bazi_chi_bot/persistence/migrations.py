"""Append-only schema history. Never edit an applied migration."""

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT NOT NULL,
        last_name TEXT,
        display_name TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );

    CREATE TABLE user_stats (
        telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
        games_played INTEGER NOT NULL DEFAULT 0 CHECK (games_played >= 0),
        wins INTEGER NOT NULL DEFAULT 0 CHECK (wins >= 0),
        losses INTEGER NOT NULL DEFAULT 0 CHECK (losses >= 0),
        correct_guesses INTEGER NOT NULL DEFAULT 0 CHECK (correct_guesses >= 0),
        wrong_guesses INTEGER NOT NULL DEFAULT 0 CHECK (wrong_guesses >= 0),
        points_won INTEGER NOT NULL DEFAULT 0 CHECK (points_won >= 0)
    );

    CREATE TABLE games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invite_token TEXT NOT NULL UNIQUE,
        creator_id INTEGER NOT NULL REFERENCES users(telegram_id),
        player2_id INTEGER REFERENCES users(telegram_id),
        fists INTEGER NOT NULL CHECK (fists BETWEEN 2 AND 6),
        total_hands INTEGER NOT NULL CHECK (total_hands IN (3, 5, 7, 9)),
        hand_number INTEGER NOT NULL DEFAULT 1 CHECK (hand_number >= 1),
        first_hider_id INTEGER REFERENCES users(telegram_id),
        hider_id INTEGER REFERENCES users(telegram_id),
        guesser_id INTEGER REFERENCES users(telegram_id),
        hidden_fist INTEGER,
        player1_score INTEGER NOT NULL DEFAULT 0 CHECK (player1_score >= 0),
        player2_score INTEGER NOT NULL DEFAULT 0 CHECK (player2_score >= 0),
        status TEXT NOT NULL DEFAULT 'waiting'
            CHECK (status IN ('waiting', 'active', 'choice', 'finished', 'cancelled')),
        phase TEXT NOT NULL DEFAULT 'waiting'
            CHECK (phase IN ('waiting', 'hiding', 'guessing', 'choice', 'finished', 'cancelled')),
        winner_id INTEGER REFERENCES users(telegram_id),
        loser_id INTEGER REFERENCES users(telegram_id),
        final_choice TEXT CHECK (final_choice IS NULL OR final_choice IN ('truth', 'dare')),
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        CHECK (player2_id IS NULL OR player2_id != creator_id),
        CHECK (hidden_fist IS NULL OR hidden_fist BETWEEN 1 AND fists)
    );

    CREATE INDEX idx_games_creator_status ON games(creator_id, status);
    CREATE INDEX idx_games_player2_status ON games(player2_id, status);
    CREATE INDEX idx_games_invite_token ON games(invite_token);
    """,
    """
    ALTER TABLE games ADD COLUMN final_prompt_text TEXT;
    ALTER TABLE games ADD COLUMN final_response_text TEXT;
    """,
    """
    CREATE TABLE countdowns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id INTEGER NOT NULL REFERENCES users(telegram_id),
        target_user_id INTEGER NOT NULL REFERENCES users(telegram_id),
        target_at INTEGER NOT NULL,
        next_run_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'completed', 'cancelled')),
        created_at INTEGER NOT NULL,
        last_sent_at INTEGER,
        completed_at INTEGER
    );

    CREATE UNIQUE INDEX idx_countdowns_one_active_per_target
        ON countdowns(target_user_id) WHERE status = 'active';
    CREATE INDEX idx_countdowns_due
        ON countdowns(status, next_run_at);
    """,
    """
    ALTER TABLE games ADD COLUMN game_type TEXT NOT NULL DEFAULT 'gol_ya_pooch'
        CHECK (game_type IN ('gol_ya_pooch', 'tic_tac_toe'));
    ALTER TABLE games ADD COLUMN board TEXT NOT NULL DEFAULT '.........'
        CHECK (length(board) = 9 AND board NOT GLOB '*[^.XO]*');
    ALTER TABLE games ADD COLUMN next_player_id INTEGER REFERENCES users(telegram_id);
    ALTER TABLE games ADD COLUMN round_starter_id INTEGER REFERENCES users(telegram_id);
    """,
    """
    CREATE TABLE questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('truth', 'dare')),
        text TEXT NOT NULL CHECK (length(trim(text)) BETWEEN 1 AND 3000),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        UNIQUE (kind, text)
    );
    ALTER TABLE games ADD COLUMN final_question_id INTEGER REFERENCES questions(id);
    CREATE TABLE question_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL UNIQUE REFERENCES games(id),
        question_id INTEGER NOT NULL REFERENCES questions(id),
        respondent_id INTEGER NOT NULL REFERENCES users(telegram_id),
        opponent_id INTEGER NOT NULL REFERENCES users(telegram_id),
        question_text TEXT NOT NULL,
        response_text TEXT,
        asked_at INTEGER NOT NULL,
        answered_at INTEGER,
        CHECK (respondent_id != opponent_id),
        UNIQUE (question_id, respondent_id, opponent_id)
    );
    CREATE INDEX idx_questions_kind_active ON questions(kind, active);
    """,
    """
    ALTER TABLE games ADD COLUMN final_response_approved INTEGER
        CHECK (final_response_approved IN (0, 1));
    ALTER TABLE games ADD COLUMN final_reviewed_at INTEGER;
    """,
    """
    CREATE TABLE score_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(telegram_id),
        game_id INTEGER REFERENCES games(id),
        reason TEXT NOT NULL CHECK (reason IN ('legacy', 'round', 'challenge')),
        hand_number INTEGER,
        amount INTEGER NOT NULL CHECK (amount > 0),
        created_at INTEGER NOT NULL,
        CHECK (
            (reason = 'legacy' AND game_id IS NULL AND hand_number IS NULL)
            OR (reason = 'round' AND game_id IS NOT NULL AND hand_number IS NOT NULL
                AND hand_number >= 1 AND amount = 1)
            OR (reason = 'challenge' AND game_id IS NOT NULL AND hand_number IS NULL AND amount = 1)
        )
    );
    CREATE UNIQUE INDEX idx_score_legacy ON score_events(user_id) WHERE reason = 'legacy';
    CREATE UNIQUE INDEX idx_score_round ON score_events(game_id, hand_number) WHERE reason = 'round';
    CREATE UNIQUE INDEX idx_score_challenge ON score_events(game_id) WHERE reason = 'challenge';
    CREATE INDEX idx_score_user ON score_events(user_id, id);

    -- Older releases retained totals, not round histories. Preserve these as opening balances.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats WHERE points_won > 0;

    CREATE TRIGGER score_event_updates_total AFTER INSERT ON score_events
    BEGIN
        UPDATE user_stats SET points_won = points_won + NEW.amount WHERE telegram_id = NEW.user_id;
    END;
    CREATE TRIGGER score_event_valid_player BEFORE INSERT ON score_events
    WHEN NEW.game_id IS NOT NULL
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM games g WHERE g.id = NEW.game_id
                AND NEW.user_id IN (g.creator_id, g.player2_id)
        ) THEN RAISE(ABORT, 'Score recipient is not a player') END;
    END;
    CREATE TRIGGER score_event_no_update BEFORE UPDATE ON score_events
    BEGIN
        SELECT RAISE(ABORT, 'Score events are immutable');
    END;
    CREATE TRIGGER score_event_no_delete BEFORE DELETE ON score_events
    BEGIN
        SELECT RAISE(ABORT, 'Score events are immutable');
    END;

    CREATE INDEX idx_games_pending_prompt ON games(winner_id, updated_at DESC, id DESC)
        WHERE status = 'finished' AND final_choice IS NOT NULL AND final_prompt_text IS NULL;
    CREATE INDEX idx_games_pending_response ON games(loser_id, updated_at DESC, id DESC)
        WHERE status = 'finished' AND final_choice IS NOT NULL AND final_response_text IS NULL;
    CREATE INDEX idx_games_pending_review ON games(winner_id, updated_at DESC, id DESC)
        WHERE status = 'finished' AND final_response_text IS NOT NULL AND final_response_approved IS NULL;
    CREATE INDEX idx_users_username ON users(username COLLATE NOCASE, updated_at DESC);

    CREATE TRIGGER answer_matches_game BEFORE INSERT ON question_answers
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM games g WHERE g.id = NEW.game_id
                AND g.loser_id = NEW.respondent_id AND g.winner_id = NEW.opponent_id
        ) THEN RAISE(ABORT, 'Answer participants do not match the game') END;
    END;
    """,
    """
    ALTER TABLE games ADD COLUMN challenge_kind TEXT;
    ALTER TABLE games ADD COLUMN challenge_question_id INTEGER;
    ALTER TABLE games ADD COLUMN challenge_prompt_text TEXT;
    ALTER TABLE games ADD COLUMN challenge_response_text TEXT;
    ALTER TABLE games ADD COLUMN challenge_approved INTEGER;
    ALTER TABLE games ADD COLUMN challenge_asker_id INTEGER;
    ALTER TABLE games ADD COLUMN challenge_respondent_id INTEGER;
    CREATE TABLE challenge_rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL REFERENCES games(id),
        round_number INTEGER NOT NULL CHECK (round_number >= 1),
        kind TEXT NOT NULL CHECK (kind IN ('truth', 'dare')),
        question_id INTEGER NOT NULL REFERENCES questions(id),
        asker_id INTEGER NOT NULL REFERENCES users(telegram_id),
        respondent_id INTEGER NOT NULL REFERENCES users(telegram_id),
        question_text TEXT NOT NULL,
        response_text TEXT,
        approved INTEGER CHECK (approved IN (0, 1)),
        created_at INTEGER NOT NULL,
        answered_at INTEGER,
        reviewed_at INTEGER,
        UNIQUE (game_id, round_number),
        UNIQUE (question_id, respondent_id, asker_id)
    );
    CREATE INDEX idx_challenge_round_pending ON challenge_rounds(respondent_id, response_text);
    """,
    """
    DROP INDEX IF EXISTS idx_score_challenge;
    CREATE UNIQUE INDEX idx_score_challenge ON score_events(game_id, hand_number)
        WHERE reason = 'challenge';
    """,
    """
    -- Extend the original game_type CHECK without rebuilding the table or losing rows.
    PRAGMA writable_schema = ON;
    UPDATE sqlite_master SET sql = replace(
        sql,
        'game_type IN (''gol_ya_pooch'', ''tic_tac_toe'')',
        'game_type IN (''gol_ya_pooch'', ''tic_tac_toe'', ''truth_or_dare'')'
    ) WHERE type = 'table' AND name = 'games';
    PRAGMA schema_version = 999;
    PRAGMA writable_schema = OFF;
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
                AND s.reason = 'legacy'
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );
    """,
    """
    CREATE TABLE sponsors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('channel', 'bot')),
        title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 120),
        chat_id TEXT,
        username TEXT,
        url TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at INTEGER NOT NULL,
        CHECK (kind = 'channel' OR username IS NOT NULL)
    );
    CREATE UNIQUE INDEX idx_sponsors_channel_key ON sponsors(kind, chat_id)
        WHERE kind = 'channel';
    CREATE UNIQUE INDEX idx_sponsors_bot_key ON sponsors(kind, username)
        WHERE kind = 'bot';
    CREATE INDEX idx_sponsors_active ON sponsors(active, id);
    -- Users may have been inserted after the previous score backfill migration.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
                AND s.reason = 'legacy'
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s WHERE s.user_id = user_stats.telegram_id
    );
    """,
    """
    ALTER TABLE users ADD COLUMN profile_photo_file_id TEXT;
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
                AND s.reason = 'legacy'
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s WHERE s.user_id = user_stats.telegram_id
    );
    """,
    """
    -- Older databases required challenge hand_number to be NULL. Independent
    -- truth-or-dare needs one immutable challenge score event per round.
    DROP TRIGGER IF EXISTS score_event_updates_total;
    DROP TRIGGER IF EXISTS score_event_valid_player;
    DROP TRIGGER IF EXISTS score_event_no_update;
    DROP TRIGGER IF EXISTS score_event_no_delete;
    DROP INDEX IF EXISTS idx_score_legacy;
    DROP INDEX IF EXISTS idx_score_round;
    DROP INDEX IF EXISTS idx_score_challenge;
    DROP INDEX IF EXISTS idx_score_user;

    ALTER TABLE score_events RENAME TO score_events_old;
    CREATE TABLE score_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(telegram_id),
        game_id INTEGER REFERENCES games(id),
        reason TEXT NOT NULL CHECK (reason IN ('legacy', 'round', 'challenge')),
        hand_number INTEGER,
        amount INTEGER NOT NULL CHECK (amount > 0),
        created_at INTEGER NOT NULL,
        CHECK (
            (reason = 'legacy' AND game_id IS NULL AND hand_number IS NULL)
            OR (reason = 'round' AND game_id IS NOT NULL AND hand_number IS NOT NULL
                AND hand_number >= 1 AND amount = 1)
            OR (reason = 'challenge' AND game_id IS NOT NULL AND hand_number IS NOT NULL
                AND hand_number >= 1 AND amount = 1)
        )
    );
    INSERT INTO score_events (id, user_id, game_id, reason, hand_number, amount, created_at)
        SELECT id, user_id, game_id, reason,
            CASE WHEN reason = 'challenge' THEN COALESCE(
                hand_number,
                (SELECT hand_number FROM games WHERE games.id = score_events_old.game_id),
                1
            ) ELSE hand_number END,
            amount, created_at
        FROM score_events_old;
    DROP TABLE score_events_old;
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
                AND s.reason = 'legacy'
        );

    CREATE UNIQUE INDEX idx_score_legacy ON score_events(user_id) WHERE reason = 'legacy';
    CREATE UNIQUE INDEX idx_score_round ON score_events(game_id, hand_number)
        WHERE reason = 'round';
    CREATE UNIQUE INDEX idx_score_challenge ON score_events(game_id, hand_number)
        WHERE reason = 'challenge';
    CREATE INDEX idx_score_user ON score_events(user_id, id);

    CREATE TRIGGER score_event_updates_total AFTER INSERT ON score_events
    BEGIN
        UPDATE user_stats SET points_won = points_won + NEW.amount WHERE telegram_id = NEW.user_id;
    END;
    CREATE TRIGGER score_event_valid_player BEFORE INSERT ON score_events
    WHEN NEW.game_id IS NOT NULL
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM games g WHERE g.id = NEW.game_id
                AND NEW.user_id IN (g.creator_id, g.player2_id)
        ) THEN RAISE(ABORT, 'Score recipient is not a player') END;
    END;
    CREATE TRIGGER score_event_no_update BEFORE UPDATE ON score_events
    BEGIN
        SELECT RAISE(ABORT, 'Score events are immutable');
    END;
    CREATE TRIGGER score_event_no_delete BEFORE DELETE ON score_events
    BEGIN
        SELECT RAISE(ABORT, 'Score events are immutable');
    END;

    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s WHERE s.user_id = user_stats.telegram_id
    );
    """,
    """
    CREATE TABLE game_messages (
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (game_id, user_id)
    );
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
                AND s.reason = 'legacy'
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s WHERE s.user_id = user_stats.telegram_id
    );
    """,
    """
    -- Keep monthly login rewards in the immutable score history while making
    -- one reward per user and calendar month idempotent.
    DROP TRIGGER IF EXISTS score_event_updates_total;
    DROP TRIGGER IF EXISTS score_event_valid_player;
    DROP TRIGGER IF EXISTS score_event_no_update;
    DROP TRIGGER IF EXISTS score_event_no_delete;
    DROP INDEX IF EXISTS idx_score_legacy;
    DROP INDEX IF EXISTS idx_score_round;
    DROP INDEX IF EXISTS idx_score_challenge;
    DROP INDEX IF EXISTS idx_score_user;

    ALTER TABLE score_events RENAME TO score_events_old;
    CREATE TABLE score_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(telegram_id),
        game_id INTEGER REFERENCES games(id),
        reason TEXT NOT NULL CHECK (reason IN ('legacy', 'round', 'challenge', 'login')),
        hand_number INTEGER,
        period_start INTEGER,
        amount INTEGER NOT NULL CHECK (amount > 0),
        created_at INTEGER NOT NULL,
        CHECK (
            (reason = 'legacy' AND game_id IS NULL AND hand_number IS NULL)
            OR (reason = 'login' AND game_id IS NULL AND hand_number IS NULL
                AND period_start IS NOT NULL AND amount = 1)
            OR (reason = 'round' AND game_id IS NOT NULL AND hand_number IS NOT NULL
                AND hand_number >= 1 AND period_start IS NULL AND amount = 1)
            OR (reason = 'challenge' AND game_id IS NOT NULL AND hand_number IS NOT NULL
                AND hand_number >= 1 AND period_start IS NULL AND amount = 1)
        )
    );
    INSERT INTO score_events (
        id, user_id, game_id, reason, hand_number, period_start, amount, created_at
    )
        SELECT id, user_id, game_id, reason, hand_number, NULL, amount, created_at
        FROM score_events_old;
    DROP TABLE score_events_old;

    CREATE UNIQUE INDEX idx_score_legacy ON score_events(user_id) WHERE reason = 'legacy';
    CREATE UNIQUE INDEX idx_score_round ON score_events(game_id, hand_number)
        WHERE reason = 'round';
    CREATE UNIQUE INDEX idx_score_challenge ON score_events(game_id, hand_number)
        WHERE reason = 'challenge';
    CREATE UNIQUE INDEX idx_score_login ON score_events(user_id, period_start)
        WHERE reason = 'login';
    CREATE INDEX idx_score_user ON score_events(user_id, id);
    CREATE INDEX idx_score_created_at ON score_events(created_at);

    CREATE TRIGGER score_event_updates_total AFTER INSERT ON score_events
    BEGIN
        UPDATE user_stats SET points_won = points_won + NEW.amount WHERE telegram_id = NEW.user_id;
    END;
    CREATE TRIGGER score_event_valid_player BEFORE INSERT ON score_events
    WHEN NEW.game_id IS NOT NULL
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM games g WHERE g.id = NEW.game_id
                AND NEW.user_id IN (g.creator_id, g.player2_id)
        ) THEN RAISE(ABORT, 'Score recipient is not a player') END;
    END;
    CREATE TRIGGER score_event_no_update BEFORE UPDATE ON score_events
    BEGIN
        SELECT RAISE(ABORT, 'Score events are immutable');
    END;
    CREATE TRIGGER score_event_no_delete BEFORE DELETE ON score_events
    BEGIN
        SELECT RAISE(ABORT, 'Score events are immutable');
    END;

    -- A database may have received users after the previous backfill migration.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
                AND s.reason = 'legacy'
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );
    """,
    """
    -- Direct truth-or-dare rounds already had an immutable score ledger, but
    -- older versions did not copy approved challenge points into the game
    -- snapshot used by the UI and winner calculation.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
                AND s.reason = 'legacy'
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );

    UPDATE games
    SET player1_score = (
            SELECT COUNT(*) FROM score_events s
            WHERE s.game_id = games.id
              AND s.reason = 'challenge'
              AND s.user_id = games.creator_id
        ),
        player2_score = (
            SELECT COUNT(*) FROM score_events s
            WHERE s.game_id = games.id
              AND s.reason = 'challenge'
              AND s.user_id = games.player2_id
        )
    WHERE game_type = 'truth_or_dare';
    """,
    """
    -- Some post-ledger upgrades treated the already-accounted score total as
    -- an opening balance. Such rows are identifiable because their amount is
    -- exactly the user's complete non-legacy ledger before that row.
    -- Preserve an aggregate-only balance from a database that was already
    -- marked as schema 16 but had not recorded any ledger rows.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );

    DROP TRIGGER IF EXISTS score_event_no_delete;
    DELETE FROM score_events
    WHERE reason = 'legacy'
      AND EXISTS (
          SELECT 1 FROM score_events earlier
          WHERE earlier.user_id = score_events.user_id
            AND earlier.id < score_events.id
            AND earlier.reason != 'legacy'
      )
      AND amount = (
          SELECT COALESCE(SUM(earlier.amount), 0) FROM score_events earlier
          WHERE earlier.user_id = score_events.user_id
            AND earlier.id < score_events.id
            AND earlier.reason != 'legacy'
      );
    CREATE TRIGGER score_event_no_delete BEFORE DELETE ON score_events
    BEGIN
        SELECT RAISE(ABORT, 'Score events are immutable');
    END;
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );

    -- A rejected direct truth-or-dare answer can leave an odd-round match
    -- tied. Older code incorrectly awarded every such match to player two.
    UPDATE user_stats
    SET wins = MAX(
            wins - (
                SELECT COUNT(*) FROM games g
                WHERE g.game_type = 'truth_or_dare'
                  AND g.status = 'finished'
                  AND g.player1_score = g.player2_score
                  AND g.winner_id = user_stats.telegram_id
            ),
            0
        ),
        losses = MAX(
            losses - (
                SELECT COUNT(*) FROM games g
                WHERE g.game_type = 'truth_or_dare'
                  AND g.status = 'finished'
                  AND g.player1_score = g.player2_score
                  AND g.loser_id = user_stats.telegram_id
            ),
            0
        );
    UPDATE games
    SET winner_id = NULL, loser_id = NULL
    WHERE game_type = 'truth_or_dare'
      AND status = 'finished'
      AND player1_score = player2_score;
    """,
    """
    -- Add the persisted state for the turn-based word guessing game and
    -- extend the original game_type CHECK without rebuilding game history.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );

    PRAGMA writable_schema = ON;
    UPDATE sqlite_master SET sql = replace(
        sql,
        'game_type IN (''gol_ya_pooch'', ''tic_tac_toe'', ''truth_or_dare'')',
        'game_type IN (''gol_ya_pooch'', ''tic_tac_toe'', ''truth_or_dare'', ''word_guess'')'
    ) WHERE type = 'table' AND name = 'games';
    PRAGMA schema_version = 2000;
    PRAGMA writable_schema = OFF;

    ALTER TABLE games ADD COLUMN word_secret TEXT;
    ALTER TABLE games ADD COLUMN word_attempts INTEGER NOT NULL DEFAULT 0
        CHECK (word_attempts >= 0);
    ALTER TABLE games ADD COLUMN word_guesses_json TEXT NOT NULL DEFAULT '[]';
    """,
    """
    -- Every Telegram user gets a short, stable internal payment identifier.
    -- Preserve aggregate-only scores from databases already marked as schema 18.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );

    ALTER TABLE users ADD COLUMN id INTEGER;
    UPDATE users
    SET id = (
        SELECT COUNT(*) FROM users AS earlier
        WHERE earlier.rowid <= users.rowid
    );
    CREATE UNIQUE INDEX idx_users_internal_id ON users(id);

    ALTER TABLE users ADD COLUMN is_activated INTEGER NOT NULL DEFAULT 0
        CHECK (is_activated IN (0, 1));
    ALTER TABLE users ADD COLUMN activation_approved_at INTEGER;
    ALTER TABLE users ADD COLUMN activation_approved_by INTEGER;

    CREATE TABLE payment_settings (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        base_amount_toman INTEGER NOT NULL CHECK (base_amount_toman > 0),
        card_number TEXT NOT NULL CHECK (length(trim(card_number)) BETWEEN 8 AND 32),
        card_holder TEXT NOT NULL CHECK (length(trim(card_holder)) BETWEEN 2 AND 100),
        updated_at INTEGER NOT NULL,
        updated_by INTEGER
    );
    INSERT INTO payment_settings (
        singleton_id, base_amount_toman, card_number, card_holder, updated_at
    ) VALUES (1, 100000, '6219861814466156', 'محمد صادق کیومرثی', unixepoch());

    CREATE TABLE payment_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
        user_internal_id INTEGER NOT NULL,
        expected_amount_toman INTEGER NOT NULL CHECK (expected_amount_toman > 0),
        receipt_type TEXT NOT NULL CHECK (receipt_type IN ('text', 'photo')),
        receipt_text TEXT,
        telegram_file_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'rejected')),
        submitted_at INTEGER NOT NULL,
        reviewed_at INTEGER,
        reviewed_by INTEGER,
        CHECK (
            (receipt_type = 'text' AND receipt_text IS NOT NULL AND telegram_file_id IS NULL)
            OR
            (receipt_type = 'photo' AND telegram_file_id IS NOT NULL)
        )
    );
    CREATE INDEX idx_payment_receipts_user_status
        ON payment_receipts(user_telegram_id, status, submitted_at DESC);
    """,
    """
    -- Keep an invitation while a new user completes payment activation.
    -- Preserve any aggregate-only score imported while schema 19 was current.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );
    CREATE TEMP TABLE pending_user_ids (user_rowid INTEGER PRIMARY KEY, new_id INTEGER NOT NULL);
    INSERT INTO pending_user_ids (user_rowid, new_id)
        SELECT rowid,
               (SELECT COALESCE(MAX(id), 0) FROM users)
                   + ROW_NUMBER() OVER (ORDER BY rowid)
        FROM users
        WHERE id IS NULL;
    UPDATE users
    SET id = (SELECT new_id FROM pending_user_ids WHERE user_rowid = users.rowid)
    WHERE id IS NULL;
    DROP TABLE pending_user_ids;
    ALTER TABLE users ADD COLUMN pending_invite_token TEXT;
    """,
    """
    -- A stable in-bot name starts from the current Telegram display name and
    -- stops following Telegram only after the user explicitly customizes it.
    -- Preserve an aggregate-only score from a database already marked as
    -- schema 20 but missing its score ledger.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );
    CREATE TEMP TABLE nickname_pending_user_ids (
        user_rowid INTEGER PRIMARY KEY,
        new_id INTEGER NOT NULL
    );
    INSERT INTO nickname_pending_user_ids (user_rowid, new_id)
        SELECT rowid,
               (SELECT COALESCE(MAX(id), 0) FROM users)
                   + ROW_NUMBER() OVER (ORDER BY rowid)
        FROM users
        WHERE id IS NULL;
    UPDATE users
    SET id = (
        SELECT new_id FROM nickname_pending_user_ids
        WHERE user_rowid = users.rowid
    )
    WHERE id IS NULL;
    DROP TABLE nickname_pending_user_ids;

    ALTER TABLE users ADD COLUMN nickname TEXT
        CHECK (nickname IS NULL OR length(trim(nickname)) BETWEEN 1 AND 40);
    ALTER TABLE users ADD COLUMN nickname_is_custom INTEGER NOT NULL DEFAULT 0
        CHECK (nickname_is_custom IN (0, 1));
    UPDATE users SET nickname = display_name WHERE nickname IS NULL;
    """,
    """
    -- Persist the color-code game without changing existing matches.
    PRAGMA writable_schema = ON;
    UPDATE sqlite_master SET sql = replace(
        sql,
        'game_type IN (''gol_ya_pooch'', ''tic_tac_toe'', ''truth_or_dare'', ''word_guess'')',
        'game_type IN (''gol_ya_pooch'', ''tic_tac_toe'', ''truth_or_dare'', ''word_guess'', ''mastermind'')'
    ) WHERE type = 'table' AND name = 'games';
    UPDATE sqlite_master SET sql = replace(
        sql,
        'fists BETWEEN 2 AND 6',
        '(fists BETWEEN 2 AND 6 OR fists = 8)'
    ) WHERE type = 'table' AND name = 'games';
    PRAGMA schema_version = 2100;
    PRAGMA writable_schema = OFF;

    ALTER TABLE games ADD COLUMN mastermind_secret_json TEXT;
    ALTER TABLE games ADD COLUMN mastermind_attempts INTEGER NOT NULL DEFAULT 0
        CHECK (mastermind_attempts >= 0);
    ALTER TABLE games ADD COLUMN mastermind_guesses_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE games ADD COLUMN mastermind_draft_json TEXT NOT NULL DEFAULT '[]';
    """,
    """
    -- Schema 21 could contain aggregate-only scores added after its earlier
    -- backfill. Preserve those balances when upgrading existing installations.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );

    -- The internal payment id can also be NULL for users imported into an
    -- already-upgraded database. Assign only missing ids without renumbering
    -- existing users.
    CREATE TEMP TABLE missing_payment_ids (
        user_rowid INTEGER PRIMARY KEY,
        new_id INTEGER NOT NULL
    );
    INSERT INTO missing_payment_ids (user_rowid, new_id)
        SELECT rowid,
               (SELECT COALESCE(MAX(id), 0) FROM users)
                   + ROW_NUMBER() OVER (ORDER BY rowid)
        FROM users
        WHERE id IS NULL;
    UPDATE users
    SET id = (SELECT new_id FROM missing_payment_ids WHERE user_rowid = users.rowid)
    WHERE id IS NULL;
    DROP TABLE missing_payment_ids;
    """,
    """
    -- Solo matches have a durable mode flag so old two-player matches keep
    -- their original rules. The virtual user is created only when needed.
    ALTER TABLE games ADD COLUMN is_solo INTEGER NOT NULL DEFAULT 0
        CHECK (is_solo IN (0, 1));
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );
    CREATE TEMP TABLE solo_migration_missing_ids (
        user_rowid INTEGER PRIMARY KEY, new_id INTEGER NOT NULL
    );
    INSERT INTO solo_migration_missing_ids (user_rowid, new_id)
        SELECT rowid,
               (SELECT COALESCE(MAX(id), 0) FROM users)
                   + ROW_NUMBER() OVER (ORDER BY rowid)
        FROM users WHERE id IS NULL;
    UPDATE users SET id = (
        SELECT new_id FROM solo_migration_missing_ids
        WHERE user_rowid = users.rowid
    ) WHERE id IS NULL;
    DROP TABLE solo_migration_missing_ids;
    """,
    """
    -- One shared challenge specification and one attempt per person/day.
    CREATE TABLE daily_challenges (
        challenge_date TEXT PRIMARY KEY,
        starts_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,
        game_type TEXT NOT NULL CHECK (game_type IN
            ('gol_ya_pooch', 'tic_tac_toe', 'word_guess', 'mastermind')),
        fists INTEGER NOT NULL,
        total_hands INTEGER NOT NULL,
        bot_starts INTEGER NOT NULL CHECK (bot_starts IN (0, 1)),
        secrets_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        end_queued_at INTEGER,
        CHECK (ends_at > starts_at)
    );
    ALTER TABLE games ADD COLUMN daily_challenge_date TEXT
        REFERENCES daily_challenges(challenge_date);
    CREATE UNIQUE INDEX idx_games_daily_attempt
        ON games(daily_challenge_date, creator_id)
        WHERE daily_challenge_date IS NOT NULL;
    CREATE INDEX idx_games_daily_result
        ON games(daily_challenge_date, creator_id, status, winner_id);

    CREATE TABLE daily_notifications (
        challenge_date TEXT NOT NULL REFERENCES daily_challenges(challenge_date),
        user_id INTEGER NOT NULL REFERENCES users(telegram_id),
        event TEXT NOT NULL CHECK (event IN ('start', 'end')),
        delivered_at INTEGER,
        PRIMARY KEY (challenge_date, user_id, event)
    );
    CREATE INDEX idx_daily_notifications_pending
        ON daily_notifications(event, delivered_at, challenge_date);

    -- Reuse the countdown delivery queue for opt-in daily reminders without
    -- replacing an admin-created countdown for the same person.
    ALTER TABLE countdowns ADD COLUMN kind TEXT NOT NULL DEFAULT 'standard'
        CHECK (kind IN ('standard', 'daily_reminder'));
    DROP INDEX idx_countdowns_one_active_per_target;
    CREATE UNIQUE INDEX idx_countdowns_one_active_per_target
        ON countdowns(target_user_id)
        WHERE status = 'active' AND kind = 'standard';
    CREATE UNIQUE INDEX idx_countdowns_one_daily_reminder
        ON countdowns(target_user_id)
        WHERE status = 'active' AND kind = 'daily_reminder';

    -- Preserve aggregate-only balances/users inserted into schema 24.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );
    CREATE TEMP TABLE daily_migration_missing_ids (
        user_rowid INTEGER PRIMARY KEY, new_id INTEGER NOT NULL
    );
    INSERT INTO daily_migration_missing_ids (user_rowid, new_id)
        SELECT rowid,
               (SELECT COALESCE(MAX(id), 0) FROM users)
                   + ROW_NUMBER() OVER (ORDER BY rowid)
        FROM users WHERE id IS NULL;
    UPDATE users SET id = (
        SELECT new_id FROM daily_migration_missing_ids
        WHERE user_rowid = users.rowid
    ) WHERE id IS NULL;
    DROP TABLE daily_migration_missing_ids;
    """,
    """
    -- Extend the game and daily challenge type checks without rebuilding live tables.
    PRAGMA writable_schema = ON;
    UPDATE sqlite_master SET sql = replace(
        sql,
        'game_type IN (''gol_ya_pooch'', ''tic_tac_toe'', ''truth_or_dare'', ''word_guess'', ''mastermind'')',
        'game_type IN (''gol_ya_pooch'', ''tic_tac_toe'', ''truth_or_dare'', ''word_guess'', ''mastermind'', ''rock_paper_scissors'')'
    ) WHERE type = 'table' AND name = 'games';
    UPDATE sqlite_master SET sql = replace(
        sql,
        '''gol_ya_pooch'', ''tic_tac_toe'', ''word_guess'', ''mastermind''',
        '''gol_ya_pooch'', ''tic_tac_toe'', ''word_guess'', ''mastermind'', ''rock_paper_scissors'''
    ) WHERE type = 'table' AND name = 'daily_challenges';
    PRAGMA schema_version = 2200;
    PRAGMA writable_schema = OFF;

    ALTER TABLE games ADD COLUMN rps_creator_move TEXT
        CHECK (rps_creator_move IS NULL OR rps_creator_move IN ('rock', 'paper', 'scissors'));
    ALTER TABLE games ADD COLUMN rps_player2_move TEXT
        CHECK (rps_player2_move IS NULL OR rps_player2_move IN ('rock', 'paper', 'scissors'));

    CREATE TABLE choice_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        hand_number INTEGER NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(telegram_id),
        game_type TEXT NOT NULL CHECK (game_type IN ('gol_ya_pooch', 'rock_paper_scissors')),
        choice TEXT NOT NULL,
        previous_choice TEXT,
        option_count INTEGER NOT NULL,
        hand_bucket INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE (game_id, hand_number, user_id)
    );
    CREATE INDEX idx_choice_observations_prediction
        ON choice_observations(game_type, option_count, id DESC);
    CREATE INDEX idx_choice_observations_user
        ON choice_observations(user_id, game_type, option_count, id DESC);

    -- Schema 25 installations may contain aggregate-only balances added after
    -- the earlier backfill; keep those balances when upgrading to this schema.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );
    CREATE TEMP TABLE rps_migration_missing_ids (
        user_rowid INTEGER PRIMARY KEY, new_id INTEGER NOT NULL
    );
    INSERT INTO rps_migration_missing_ids (user_rowid, new_id)
        SELECT rowid,
               (SELECT COALESCE(MAX(id), 0) FROM users)
                   + ROW_NUMBER() OVER (ORDER BY rowid)
        FROM users WHERE id IS NULL;
    UPDATE users SET id = (
        SELECT new_id FROM rps_migration_missing_ids
        WHERE user_rowid = users.rowid
    ) WHERE id IS NULL;
    DROP TABLE rps_migration_missing_ids;
    """,
    """
    CREATE TABLE tournaments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id INTEGER NOT NULL REFERENCES users(telegram_id),
        player2_id INTEGER REFERENCES users(telegram_id),
        is_solo INTEGER NOT NULL CHECK (is_solo IN (0, 1)),
        game_types_json TEXT NOT NULL,
        current_stage INTEGER NOT NULL DEFAULT 1,
        current_game_id INTEGER,
        player1_score INTEGER NOT NULL DEFAULT 0,
        player2_score INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL CHECK (status IN ('waiting', 'active', 'finished', 'cancelled')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE UNIQUE INDEX idx_tournaments_one_open_creator
        ON tournaments(creator_id) WHERE status IN ('waiting', 'active');
    CREATE INDEX idx_tournaments_player2_status
        ON tournaments(player2_id, status);
    ALTER TABLE games ADD COLUMN tournament_id INTEGER REFERENCES tournaments(id);
    ALTER TABLE games ADD COLUMN tournament_stage INTEGER;
    CREATE UNIQUE INDEX idx_games_tournament_stage
        ON games(tournament_id, tournament_stage) WHERE tournament_id IS NOT NULL;

    -- Preserve aggregate-only values written after the previous migration.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );
    CREATE TEMP TABLE tournament_migration_missing_ids (
        user_rowid INTEGER PRIMARY KEY, new_id INTEGER NOT NULL
    );
    INSERT INTO tournament_migration_missing_ids (user_rowid, new_id)
        SELECT rowid,
               (SELECT COALESCE(MAX(id), 0) FROM users)
                   + ROW_NUMBER() OVER (ORDER BY rowid)
        FROM users WHERE id IS NULL;
    UPDATE users SET id = (
        SELECT new_id FROM tournament_migration_missing_ids
        WHERE user_rowid = users.rowid
    ) WHERE id IS NULL;
    DROP TABLE tournament_migration_missing_ids;
    """,
    """
    CREATE TABLE group_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        creator_id INTEGER NOT NULL REFERENCES users(telegram_id),
        game_type TEXT NOT NULL CHECK (game_type IN (
            'rock_paper_scissors', 'gol_ya_pooch', 'tic_tac_toe',
            'word_guess', 'mastermind', 'truth_or_dare')),
        total_rounds INTEGER NOT NULL DEFAULT 0 CHECK (total_rounds >= 0),
        current_round INTEGER NOT NULL DEFAULT 1,
        phase TEXT NOT NULL DEFAULT 'waiting',
        board TEXT NOT NULL DEFAULT '.........',
        secret_choice TEXT,
        prompt_text TEXT,
        answer_text TEXT,
        status TEXT NOT NULL DEFAULT 'waiting'
            CHECK (status IN ('waiting', 'active', 'finished', 'cancelled')),
        last_result TEXT,
        created_at INTEGER NOT NULL
    );
    CREATE UNIQUE INDEX idx_group_sessions_open_chat ON group_sessions(chat_id)
        WHERE status IN ('waiting', 'active');
    CREATE TABLE group_players (
        session_id INTEGER NOT NULL REFERENCES group_sessions(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(telegram_id),
        display_name TEXT NOT NULL,
        score INTEGER NOT NULL DEFAULT 0 CHECK (score >= 0),
        joined_at INTEGER NOT NULL,
        PRIMARY KEY (session_id, user_id)
    );
    CREATE TABLE group_round_moves (
        session_id INTEGER NOT NULL REFERENCES group_sessions(id) ON DELETE CASCADE,
        round_number INTEGER NOT NULL,
        phase TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        choice TEXT NOT NULL,
        PRIMARY KEY (session_id, round_number, phase, user_id),
        FOREIGN KEY (session_id, user_id) REFERENCES group_players(session_id, user_id)
    );
    CREATE TABLE group_attempts (
        session_id INTEGER NOT NULL REFERENCES group_sessions(id) ON DELETE CASCADE,
        round_number INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        attempt_number INTEGER NOT NULL,
        guess TEXT NOT NULL,
        feedback TEXT NOT NULL,
        PRIMARY KEY (session_id, round_number, user_id, attempt_number),
        FOREIGN KEY (session_id, user_id) REFERENCES group_players(session_id, user_id)
    );

    -- Keep aggregate-only balances introduced after the previous migration.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );
    CREATE TEMP TABLE group_migration_missing_ids (
        user_rowid INTEGER PRIMARY KEY, new_id INTEGER NOT NULL
    );
    INSERT INTO group_migration_missing_ids (user_rowid, new_id)
        SELECT rowid,
               (SELECT COALESCE(MAX(id), 0) FROM users)
                   + ROW_NUMBER() OVER (ORDER BY rowid)
        FROM users WHERE id IS NULL;
    UPDATE users SET id = (
        SELECT new_id FROM group_migration_missing_ids
        WHERE user_rowid = users.rowid
    ) WHERE id IS NULL;
    DROP TABLE group_migration_missing_ids;
    """,
    """
    CREATE TABLE human_word_submissions (
        word TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(telegram_id),
        created_at INTEGER NOT NULL,
        PRIMARY KEY (word, user_id)
    );
    CREATE INDEX idx_human_word_submissions_user
        ON human_word_submissions(user_id, word);

    CREATE TABLE bot_word_assignments (
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        hand_number INTEGER NOT NULL CHECK (hand_number >= 1),
        user_id INTEGER NOT NULL REFERENCES users(telegram_id),
        word TEXT NOT NULL,
        assigned_at INTEGER NOT NULL,
        PRIMARY KEY (game_id, hand_number),
        UNIQUE (user_id, word)
    );
    CREATE INDEX idx_bot_word_assignments_user
        ON bot_word_assignments(user_id, assigned_at);

    -- Historical games retain the latest secret; backfill what is still available.
    INSERT OR IGNORE INTO human_word_submissions (word, user_id, created_at)
        SELECT word_secret, hider_id, updated_at FROM games
        WHERE game_type = 'word_guess' AND word_secret IS NOT NULL
            AND hider_id IS NOT NULL AND hider_id != -1;
    INSERT OR IGNORE INTO human_word_submissions (word, user_id, created_at)
        WITH numbered_players AS (
            SELECT session_id, user_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY session_id ORDER BY joined_at, rowid
                   ) - 1 AS turn_index,
                   COUNT(*) OVER (PARTITION BY session_id) AS player_count
            FROM group_players
        )
        SELECT s.secret_choice, p.user_id, s.created_at
        FROM group_sessions s
        JOIN numbered_players p ON p.session_id = s.id
            AND p.turn_index = (s.current_round - 1) % p.player_count
        WHERE s.game_type = 'word_guess' AND s.secret_choice IS NOT NULL;
    INSERT OR IGNORE INTO bot_word_assignments
        (game_id, hand_number, user_id, word, assigned_at)
        SELECT id, hand_number, creator_id, word_secret, updated_at FROM games
        WHERE game_type = 'word_guess' AND is_solo = 1
            AND hider_id = -1 AND word_secret IS NOT NULL;

    -- Preserve aggregate-only balances/users inserted after the prior schema.
    INSERT INTO score_events (user_id, reason, amount, created_at)
        SELECT telegram_id, 'legacy', points_won, unixepoch() FROM user_stats
        WHERE points_won > 0 AND NOT EXISTS (
            SELECT 1 FROM score_events s WHERE s.user_id = user_stats.telegram_id
        );
    UPDATE user_stats SET points_won = (
        SELECT COALESCE(SUM(amount), 0) FROM score_events s
        WHERE s.user_id = user_stats.telegram_id
    );
    CREATE TEMP TABLE word_migration_missing_ids (
        user_rowid INTEGER PRIMARY KEY, new_id INTEGER NOT NULL
    );
    INSERT INTO word_migration_missing_ids (user_rowid, new_id)
        SELECT rowid,
               (SELECT COALESCE(MAX(id), 0) FROM users)
                   + ROW_NUMBER() OVER (ORDER BY rowid)
        FROM users WHERE id IS NULL;
    UPDATE users SET id = (
        SELECT new_id FROM word_migration_missing_ids
        WHERE user_rowid = users.rowid
    ) WHERE id IS NULL;
    DROP TABLE word_migration_missing_ids;
    """,
)
