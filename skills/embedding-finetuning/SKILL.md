---
name: embedding-finetuning
description: Embedding fine-tuning — model selection, fine-tuning data preparation, fine-tuning process,
  evaluation
triggers:
- embedding
- finetune
- fine-tune
- contrastive
- sentence-transformer
version: 1.0.0
schema_version: 2
skill_id: embedding-finetuning
task_families:
- research
- data-analysis
capabilities:
- performance
aliases:
- embedding-finetune
- finetune-embeddings
- contrastive-tuning
- sentence-transformer-training
stack:
  languages:
  - python
  frameworks:
  - sentence-transformers
  - openai
  independent: true
preconditions:
- Labeled (query, relevant_doc) pairs can be built
- A base embedding model has been selected
- A retrieval evaluation set exists for before/after comparison
required_tools:
- python
- sentence-transformers
exclusions:
- Only chunking or prompt changes are needed
- No evaluation data exists to prove retrieval improvement
- Choosing or tuning a vector database (use postgresql-pgvector)
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: key-resources
  heading: Key Resources
  stage: inspect
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Embedding Fine-Tuning

## Core Skills

| Skill | Practice |
|---|---|
| Model selection | Compare text-embedding-3-small vs nomic-embed-text |
| Data preparation | Build (query, relevant_doc) pairs from context |
| Fine-tuning process | Fine-tune with contrastive learning |
| Evaluation | Evaluate Recall@k and MRR before/after |

## Key Resources
- [sentence-transformers](https://www.sbert.net/)
- [OpenAI Fine-tuning](https://platform.openai.com/docs/guides/fine-tuning)

## Practice Project
Fine-tune an embedding model on Machina context chunks. Measure retrieval quality improvement.
