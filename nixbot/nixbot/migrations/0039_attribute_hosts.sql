-- Where an attribute was evaluated (reported by the evaluator, e.g. a
-- remote-evaluating nix-eval-jobs wrapper) and where its own derivation
-- was built (the machine of nix's build activity). NULL when unknown,
-- substituted or never built.
ALTER TABLE build_attributes
    ADD COLUMN eval_host TEXT,
    ADD COLUMN build_host TEXT;
