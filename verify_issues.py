"""Verification script for code quality issues."""
import ast
import sys
from pathlib import Path

def check_dead_functions():
    """Check for dead functions that should be removed."""
    dead = []
    
    # Actually dead functions (no usages found)
    actually_dead = [
        ("ah/core/config.py", "set_session_override"),
        ("ah/core/config.py", "clear_session_override"),
        ("ah/core/config.py", "clear_all_session_overrides"),
        ("ah/core/context.py", "enforce_budget"),
        ("ah/core/metrics.py", "setup_structured_logging"),
        ("ah/core/session.py", "set_status"),
        ("ah/memory/approval.py", "get_pending"),
        ("ah/rag/embedder.py", "clear_cache"),
        ("ah/services.py", "parse_session_id"),
        ("ah/tools/rag.py", "set_rag_pipeline"),
    ]
    
    for filepath, func_name in actually_dead:
        path = Path(filepath)
        if not path.exists():
            continue
        content = path.read_text()
        # Check if function is defined
        if f"def {func_name}(" not in content:
            continue
        # Check if it's used elsewhere (excluding the definition)
        lines = content.split('\n')
        usage_count = 0
        for i, line in enumerate(lines):
            if f"def {func_name}(" in line:
                continue
            if func_name in line:
                usage_count += 1
        if usage_count == 0:
            dead.append(f"{filepath}:{func_name}")
    
    return dead

def check_circular_imports():
    """Check for circular imports between gateway/server and features."""
    # Check if features import from server at module level
    issues = []
    features_dir = Path("ah/gateway/features")
    for f in features_dir.glob("*.py"):
        content = f.read_text()
        # Check for module-level imports from server
        lines = content.split('\n')
        in_type_checking = False
        for line in lines:
            if 'if TYPE_CHECKING' in line:
                in_type_checking = True
            if in_type_checking and 'from ah.gateway.server import' in line:
                continue  # This is OK
            if not in_type_checking and 'from ah.gateway.server import' in line:
                issues.append(f"{f.name}: module-level import from server")
    return issues

def check_duplicate_functions():
    """Check for duplicate function bodies."""
    duplicates = []
    
    # Check _row_to_chunk
    context_content = Path("ah/core/context.py").read_text()
    pipeline_content = Path("ah/rag/pipeline.py").read_text()
    if "_row_to_chunk" in context_content and "_row_to_chunk" in pipeline_content:
        duplicates.append("_row_to_chunk in context.py and pipeline.py")
    
    # Check provider close methods
    provider_content = Path("ah/core/provider.py").read_text()
    close_count = provider_content.count("async def close(self)")
    if close_count > 1:
        duplicates.append(f"provider.py has {close_count} close() methods")
    
    # Check embedder/reranker model_name
    embedder_content = Path("ah/rag/embedder.py").read_text()
    reranker_content = Path("ah/rag/reranker.py").read_text()
    if "def model_name" in embedder_content and "def model_name" in reranker_content:
        duplicates.append("model_name in embedder.py and reranker.py")
    
    return duplicates

def check_missing_docstrings():
    """Check for missing docstrings in public functions."""
    missing = []
    
    files_to_check = [
        "ah/api/app.py",
        "ah/gateway/features/agents.py",
        "ah/gateway/features/config.py",
        "ah/gateway/features/jobs.py",
        "ah/gateway/features/memory.py",
        "ah/gateway/features/sessions.py",
        "ah/gateway/features/skills.py",
        "ah/core/container.py",
        "ah/core/scheduler.py",
        "ah/core/usage.py",
        "ah/memory/policy.py",
        "ah/plugins/base.py",
        "ah/plugins/registry.py",
    ]
    
    for filepath in files_to_check:
        path = Path(filepath)
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Skip private functions
                if node.name.startswith('_'):
                    continue
                # Check if it has a docstring
                if not ast.get_docstring(node):
                    missing.append(f"{filepath}:{node.lineno}:{node.name}")
    
    return missing

if __name__ == "__main__":
    print("=== Dead Functions ===")
    dead = check_dead_functions()
    for d in dead:
        print(f"  {d}")
    print(f"Total: {len(dead)}")
    
    print("\n=== Circular Imports ===")
    circular = check_circular_imports()
    for c in circular:
        print(f"  {c}")
    print(f"Total: {len(circular)}")
    
    print("\n=== Duplicate Functions ===")
    dupes = check_duplicate_functions()
    for d in dupes:
        print(f"  {d}")
    print(f"Total: {len(dupes)}")
    
    print("\n=== Missing Docstrings ===")
    missing = check_missing_docstrings()
    for m in missing:
        print(f"  {m}")
    print(f"Total: {len(missing)}")
