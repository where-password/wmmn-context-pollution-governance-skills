from __future__ import annotations
import hashlib,json,shutil,zipfile
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/"release"; OUT.mkdir(exist_ok=True)
IGNORE=shutil.ignore_patterns("tests","work",".venv",".idea",".loop-agent","__pycache__","*.pyc","*.sqlite3","*.sqlite3-*",".env",".secrets")
RUNTIME=("SKILL.md","agents","assets","references","scripts","vendor","pyproject.toml",".gitignore","README.md")
def version(skill):
    for line in (skill/"pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("version = "): return line.split("=",1)[1].strip().strip('"')
    raise RuntimeError("version missing")
def sha(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for c in iter(lambda:f.read(1048576),b""): h.update(c)
    return h.hexdigest()
def build(skill):
    ver=version(skill); stage=OUT/f"{skill.name}-{ver}"
    if stage.exists(): shutil.rmtree(stage)
    stage.mkdir()
    for name in RUNTIME:
        src=skill/name
        if src.is_dir(): shutil.copytree(src,stage/name,ignore=IGNORE)
        elif src.is_file(): shutil.copy2(src,stage/name)
    files=[]
    for path in sorted(stage.rglob("*")):
        if path.is_file(): files.append({"path":path.relative_to(stage).as_posix(),"size":path.stat().st_size,"sha256":sha(path)})
    (stage/"PACKAGE_MANIFEST.json").write_text(json.dumps({"created_at":datetime.now(timezone.utc).isoformat(),"files":files},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (stage/"SHA256SUMS.txt").write_text("".join(f"{x['sha256']}  {x['path']}\n" for x in files),encoding="utf-8")
    archive=OUT/f"{skill.name}-{ver}.zip"
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED) as z:
        for path in sorted(stage.rglob("*")):
            if path.is_file(): z.write(path,arcname=f"{stage.name}/{path.relative_to(stage).as_posix()}")
    archive.with_suffix(archive.suffix+".sha256").write_text(f"{sha(archive)}  {archive.name}\n",encoding="utf-8")
    return archive
if __name__=="__main__":
    for skill in sorted((ROOT/"skills").iterdir()):
        if skill.is_dir(): print(build(skill))
