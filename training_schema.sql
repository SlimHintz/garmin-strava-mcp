CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id TEXT UNIQUE,
    date DATE,
    week INTEGER,
    phase TEXT,
    name TEXT,
    distance_mi REAL,
    moving_time_sec INTEGER,
    avg_hr INTEGER,
    max_hr INTEGER,
    avg_pace_sec_per_mi INTEGER,
    shoe TEXT,
    weather_temp REAL,
    aqi INTEGER,
    training_load REAL,
    training_effect_label TEXT,
    vo2max REAL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS session_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id TEXT UNIQUE,
    avg_gct_ms REAL,
    avg_cadence REAL,
    avg_stride_cm REAL,
    avg_vertical_osc_cm REAL,
    avg_vertical_ratio REAL,
    avg_respiration REAL,
    FOREIGN KEY (activity_id) REFERENCES runs(activity_id)
);

CREATE TABLE IF NOT EXISTS intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id TEXT,
    interval_type TEXT,
    interval_number INTEGER,
    distance_mi REAL,
    time_sec INTEGER,
    avg_pace_actual_sec_per_mi INTEGER,
    avg_hr INTEGER,
    max_hr INTEGER,
    avg_gct_ms REAL,
    avg_cadence REAL,
    avg_stride_cm REAL,
    FOREIGN KEY (activity_id) REFERENCES runs(activity_id)
);

CREATE TABLE IF NOT EXISTS weekly_summary (
    week INTEGER PRIMARY KEY,
    phase TEXT,
    target_miles REAL,
    actual_miles REAL,
    tue_weight REAL,
    notes TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE,
    checkpoint_name TEXT,
    seg1_pace TEXT,
    seg1_hr INTEGER,
    seg2_pace TEXT,
    seg2_hr INTEGER,
    seg3_pace TEXT,
    seg3_hr INTEGER,
    outcome TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS form_trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id TEXT,
    date DATE,
    week INTEGER,
    session_type TEXT,
    avg_gct_ms REAL,
    avg_cadence REAL,
    avg_stride_cm REAL,
    avg_vertical_osc_cm REAL,
    avg_hr INTEGER,
    avg_pace_sec_per_mi INTEGER,
    FOREIGN KEY (activity_id) REFERENCES runs(activity_id)
);
