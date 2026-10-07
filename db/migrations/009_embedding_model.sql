BEGIN;

-- Record which embedder produced each vector so vectors from different models are never
-- compared silently. NULL means "no embedding computed" (embedder = none).
ALTER TABLE embedding_projection
  ADD COLUMN embedding_model text,
  ADD CONSTRAINT embedding_projection_model_present
    CHECK ((embedding IS NULL) = (embedding_model IS NULL));

CREATE INDEX ix_embedding_projection_model ON embedding_projection(embedding_model);

COMMIT;
