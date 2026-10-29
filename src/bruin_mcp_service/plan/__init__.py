"""The general-purpose plan engine: a structured plan → validated Bruin assets.

  model.py     the plan format (pydantic; unknown fields are rejected)
  engine.py    schema-flow validation and SQL compilation, step by step
  rawsql.py    the raw-SQL fallback, checked with sqlglot against the same schema
  pipeline.py  writing a Bruin pipeline atomically and running `bruin validate`
  saved.py     saved plans (templates) that run through the same engine
"""
