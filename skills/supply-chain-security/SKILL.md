---
name: supply-chain-security
description: Software supply chain security — SBOMs, dependency provenance, artifact signing, SLSA levels,
  pinned versions, reproducible builds
triggers:
- supply chain
- sbom
- slsa
- artifact signing
- dependency provenance
- sigstore
- reproducible build
version: 1.0.0
schema_version: 2
skill_id: supply-chain-security
task_families:
- security-review
- deployment
capabilities:
- configuration
- migration
aliases:
- sbom
- slsa
- supply-chain
- sigstore
- artifact-signing
- provenance
stack:
  languages: []
  frameworks:
  - sigstore
  - cyclonedx
  - slsa
  independent: true
preconditions:
- a build pipeline producing release artifacts
- lock files or dependency manifests to pin
- access to artifact registry and signing identity
required_tools:
- sigstore
- cyclonedx
exclusions:
- runtime exploitation or penetration testing
- license compliance auditing
- application feature development
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: pitfalls
  heading: Pitfalls
  stage: review
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Software Supply Chain Security

## Core Skills

| Skill | Practice |
|---|---|
| SBOM | Generate and retain a bill of materials per release artifact |
| Dependency provenance | Record where every direct and transitive package came from |
| Pinned versions | Lock files plus hashes for installs |
| Artifact signing | Sign builds and verify signatures before deploy |
| SLSA levels | Move build steps toward a hosted, reproducible pipeline |
| Vulnerability response | Map advisories to the SBOM and patch with evidence |
| Least privilege CI | Short-lived, scoped build credentials |

## Pitfalls
- An SBOM nobody reads is not a control; wire it to an alerting path.
- Floating version ranges make a build irreproducible and hide new code.
- Verifying at build time but not at deploy leaves the artifact open to swap.
- Transitive dependencies dominate the attack surface; enumerate them all.

## Practice Project
For one artifact: emit an SBOM, pin dependencies with hashes, sign the build, verify the signature in a deploy gate, and write the remediation steps for a hypothetical advisory in a transitive package.

## Key Resources
- [SLSA](https://slsa.dev/)
- [Sigstore](https://www.sigstore.dev/)
- [CycloneDX](https://cyclonedx.org/)
- [NIST SSDF](https://csrc.nist.gov/Projects/ssdf)
