#!/usr/bin/env python3
"""
test_block_classification.py -- Tests the three-way block classifier.

Validates that bash one-liners like 'pkill -f infinite_counter' are classified
as 'direct_cmd' (execute via SSH) rather than 'junk' (discard).

Run: python3 test_block_classification.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# We need to test classify_block without the full project imports.
# Import just the function by exec'ing the relevant section.
import re

class FakeBlock:
    def __init__(self, language, code, index=0):
        self.language = language
        self.code = code
        self.index = index

# Copy classify_block from scheduler.py for standalone testing
def classify_block(block, all_blocks=None, prompt=""):
    code = block.code.strip()
    lang = block.language.lower()
    lines = [l for l in code.split("\n") if l.strip()]
    num_lines = len(lines)

    if lang in ("txt", "text", "plaintext", "yaml", "yml"):
        return "junk"

    if lang in ("python", "py", "c", "cpp", "c++", "rust", "java", "javascript", "js"):
        if num_lines >= 3:
            return "program"
        has_logic = any(kw in code for kw in [
            "def ", "class ", "import ", "from ", "for ", "while ",
            "if ", "#include", "int main",
        ])
        if has_logic:
            return "program"
        if lang in ("python", "py") and ("import " in code or "os." in code):
            return "program"

    if lang in ("bash", "sh", ""):
        joined = " ".join(lines)
        if num_lines <= 5:
            if re.search(r"python3?\s+\S+\.py\s*&", joined):
                return "junk"
            if re.search(r"nohup\s+python", joined):
                return "junk"
            if re.search(r"\bfg\b", joined) and re.search(r"ctrl", joined, re.IGNORECASE):
                return "junk"
            if re.search(r"kill\s+\$\(cat\s+", joined):
                return "junk"
        if num_lines <= 2 and re.search(r"\b\d+\s+\d+\.\d+\s+", code):
            return "junk"
        if num_lines <= 2 and re.search(r"kill\s+(-\d+\s+)?(\d{4,5}|<PID>)", code):
            return "junk"
        if num_lines == 1 and re.search(r"^ps\s+aux\s*\|", code):
            return "junk"
        if num_lines <= 2:
            if re.search(r"^(pip\s+install|chmod\s+|sudo\s+reboot)", joined, re.IGNORECASE):
                return "junk"
        direct_patterns = [
            r"^pkill\s+(-\w+\s+)*-f\s+\S+",
            r"^pkill\s+(-\d+\s+)?\w+",
            r"^killall\s+",
            r"^systemctl\s+(stop|restart|start)",
            r"^service\s+\w+\s+(stop|restart)",
        ]
        for pat in direct_patterns:
            if re.match(pat, joined, re.IGNORECASE):
                return "direct_cmd"
        if num_lines >= 5:
            return "program"
        if any(kw in code for kw in ["for ", "while ", "if ", "function ", "#!/"]):
            return "program"
        if num_lines <= 2:
            return "junk"

    if num_lines <= 2:
        has_logic = any(kw in code for kw in [
            "def ", "class ", "import ", "from ", "for ", "while ",
            "if ", "#include", "int main", "void ", "fn ",
        ])
        if not has_logic:
            return "junk"

    return "program"


passed = 0
failed = 0

def test(name, condition):
    global passed, failed
    if condition:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name}")
        failed += 1


# ============================================================
print("=" * 60)
print("Bash one-liners that ARE actionable (should be direct_cmd)")
print("=" * 60)

test("pkill -f infinite_counter",
     classify_block(FakeBlock("bash", "pkill -f infinite_counter")) == "direct_cmd")
test("pkill -f counter.py",
     classify_block(FakeBlock("bash", "pkill -f counter.py")) == "direct_cmd")
test("pkill -9 python3",
     classify_block(FakeBlock("bash", "pkill -9 python3")) == "direct_cmd")
test("pkill -f python3",
     classify_block(FakeBlock("bash", "pkill -f python3")) == "direct_cmd")
test("killall python3",
     classify_block(FakeBlock("bash", "killall python3")) == "direct_cmd")
test("systemctl stop myservice",
     classify_block(FakeBlock("bash", "systemctl stop myservice")) == "direct_cmd")

print()
print("=" * 60)
print("Bash blocks that are JUNK (example output, tips, placeholders)")
print("=" * 60)

test("ps aux | grep python",
     classify_block(FakeBlock("bash", "ps aux | grep python")) == "junk")
test("kill 12345 (placeholder PID)",
     classify_block(FakeBlock("bash", "kill 12345")) == "junk")
test("kill -9 12345",
     classify_block(FakeBlock("bash", "kill -9 12345")) == "junk")
test("kill -9 <PID>",
     classify_block(FakeBlock("bash", "kill -9 <PID>")) == "junk")
test("python3 script.py & + echo $!",
     classify_block(FakeBlock("bash", "python3 infinite_counter.py &\necho $! > infinite_counter.pid")) == "junk")
test("text block",
     classify_block(FakeBlock("text", "scoobyxd 12345 0.3 ... python3 counter.py")) == "junk")
test("Ctrl+C",
     classify_block(FakeBlock("bash", "Ctrl+C")) == "junk")
test("pip install something",
     classify_block(FakeBlock("bash", "pip install paramiko")) == "junk")
test("sudo reboot",
     classify_block(FakeBlock("bash", "sudo reboot")) == "junk")
test("ps aux | grep infinite_counter",
     classify_block(FakeBlock("bash", "ps aux | grep infinite_counter")) == "junk")

print()
print("=" * 60)
print("Real programs (should be 'program')")
print("=" * 60)

test("Multi-line Python kill script",
     classify_block(FakeBlock("python", """import os
import signal
for pid_str in os.listdir('/proc'):
    if pid_str.isdigit():
        os.kill(int(pid_str), signal.SIGTERM)
""")) == "program")

test("Short python with import",
     classify_block(FakeBlock("python", "import subprocess\nsubprocess.run(['pkill', '-f', 'counter'])")) == "program")

test("Python try/except KeyboardInterrupt",
     classify_block(FakeBlock("python", """try:
    while True:
        pass
except KeyboardInterrupt:
    print("Stopped cleanly")
""")) == "program")

test("Multi-line bash script with logic",
     classify_block(FakeBlock("bash", """#!/bin/bash
for pid in $(pgrep -f counter); do
    kill -9 $pid
done
echo "Done"
""")) == "program")


print()
print("=" * 60)
print(f"RESULTS: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed > 0 else 0)
