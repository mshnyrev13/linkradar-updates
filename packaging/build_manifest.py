"""Source provenance for local artifacts; never includes profiles or environment."""
import hashlib
import json
from pathlib import Path

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def create(root,version):
    paths=[*root.glob('*.py'),root/'requirements-desktop.txt',*(p for p in (root/'packaging').iterdir() if p.is_file()),*(p for p in (root/'branding').iterdir() if p.is_file())]
    return dict(version=version,files={str(p.relative_to(root)):digest(p) for p in sorted(paths)})

def save(root,target,version,**extra):
    result=create(root,version);result.update(extra)
    target.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result
