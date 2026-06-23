-- Reproducibility: record exactly which model produced each step (pinned + actual-after-fallback).
ALTER TABLE traces ADD COLUMN IF NOT EXISTS model TEXT;
