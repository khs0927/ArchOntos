BEGIN;

-- Approximate nearest-neighbour index for cosine similarity search. Partial: rows without a
-- vector (embedder = none) are never candidates. Embedders emit L2-normalised vectors, so
-- cosine distance and inner product rank identically; cosine is the safer operator class.
CREATE INDEX IF NOT EXISTS hnsw_embedding_projection_cosine
  ON embedding_projection USING hnsw (embedding vector_cosine_ops)
  WHERE embedding IS NOT NULL;

COMMIT;
