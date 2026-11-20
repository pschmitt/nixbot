-- Where an attribute was evaluated (reported by the evaluator, e.g. a
-- remote-evaluating nix-eval-jobs wrapper) and where its own derivation
-- was built (the machine of nix's build activity). NULL when unknown,
-- substituted or never built.
ALTER TABLE build_attributes
    ADD COLUMN IF NOT EXISTS eval_host TEXT,
    ADD COLUMN IF NOT EXISTS build_host TEXT;
