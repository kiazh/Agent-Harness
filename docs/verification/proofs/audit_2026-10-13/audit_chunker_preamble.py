#!/usr/bin/env python
"""Audit finding: structured code splitting starts at the first definition and
discards the preamble; extension names ('py') differ from splitter language
names ('python')."""
import sys
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

from machina.rag.chunker import RecursiveCharacterTextSplitter

chunker = RecursiveCharacterTextSplitter()

go_source = """package main

import "fmt"

const Greeting = "hi"

func Alpha() { fmt.Println(Greeting) }

func Beta() {}
"""
go_chunks = chunker.split_code(go_source, language="go")
go_text = "".join(c.text for c in go_chunks)
print("go chunks:", len(go_chunks), "symbols:", [c.metadata.get("symbol") for c in go_chunks])
print("go preamble 'package main' present:", "package main" in go_text)
print("go preamble 'import \"fmt\"' present:", 'import "fmt"' in go_text)

py_source = """import os

CONST = 1

def alpha():
    return os.name

def beta():
    return CONST
"""
py_ext = chunker.split_code(py_source, language="py")
py_lang = chunker.split_code(py_source, language="python")
print("language='py'  symbols:", [c.metadata.get("symbol") for c in py_ext])
print("language='python' symbols:", [c.metadata.get("symbol") for c in py_lang])
print("language='python' keeps preamble 'import os':",
      "import os" in "".join(c.text for c in py_lang))

confirmed = (
    "package main" not in go_text
    and any(c.metadata.get("symbol") for c in go_chunks)
    and not any(c.metadata.get("symbol") for c in py_ext)
    and any(c.metadata.get("symbol") for c in py_lang)
)
print("CLAIM CONFIRMED" if confirmed else "CLAIM NOT REPRODUCED (as stated)")
sys.exit(0 if confirmed else 1)
