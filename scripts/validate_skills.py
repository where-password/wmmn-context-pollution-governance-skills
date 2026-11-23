from __future__ import annotations
import sys
from pathlib import Path
root=Path(sys.argv[1] if len(sys.argv)>1 else ".")
fail=[]
for skill in sorted((root/"skills").iterdir()):
    if not skill.is_dir(): continue
    for rel in ("SKILL.md","README.md","agents/openai.yaml"):
        if not (skill/rel).is_file(): fail.append(f"{skill.name}: missing {rel}")
    text=(skill/"SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\nname:" not in text[:1000] or "\ndescription:" not in text[:1800]: fail.append(f"{skill.name}: invalid frontmatter")
if fail: print("\n".join(fail)); raise SystemExit(1)
print("SKILLS_VALID")
