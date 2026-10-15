import sys
import tempfile
import time
from pathlib import Path
from machina.mcp_server import _run_process
with tempfile.TemporaryDirectory(prefix='machina-runner-review-') as tmp:
    marker = Path(tmp) / 'late.txt'
    code = 'import pathlib,sys,time;sys.stdout.write("x"*65536);sys.stdout.flush();time.sleep(0.35);pathlib.Path(sys.argv[1]).write_text("ran after output cap");time.sleep(0.2)'
    started = time.monotonic()
    result = _run_process([sys.executable,'-c',code,str(marker)],timeout=2,cwd=Path(tmp))
    print({'elapsed':round(time.monotonic()-started,3),'returncode':result.returncode,'truncated':result.truncated,'timed_out':result.timed_out,'late_side_effect':marker.exists(),'output_chars':len(result.output)})
