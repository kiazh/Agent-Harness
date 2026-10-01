---
name: embedding-finetuning
description: "Embedding fine-tuning — model selection, fine-tuning data preparation, fine-tuning process, evaluation"
triggers:
  - "embedding"
  - "finetune"
  - "fine-tune"
  - "contrastive"
  - "sentence-transformer"
version: 1.0.0
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
Fine-tune an embedding model on AgentHarness context chunks. Measure retrieval quality improvement.
