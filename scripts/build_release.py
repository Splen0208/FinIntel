"""Build a source-only archive from a strict allowlist, never from local runtime state."""
import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT_FILES=['README.md','LICENSE','THIRD_PARTY_NOTICES.md','SECURITY.md','CONTRIBUTING.md',
    '.gitignore','requirements.txt','requirements-platforms.txt','requirements-dev.txt',
    'finintel.py','start-server.bat','open-collect-browser.bat']
PUBLIC_DOCS=['QUICKSTART.md','ARCHITECTURE.md','RELEASE_CHECKS.md','DEMO.md']
PUBLIC_SCRIPTS=['build_release.py','open_boss_browser.py','repair_xhs_search_visibility.py']

def source_files(root):
    paths=[root/name for name in ROOT_FILES]
    paths.extend(root/'docs'/name for name in PUBLIC_DOCS)
    paths.append(root/'docs/assets/finintel-demo-preview.png')
    paths.extend(root/'scripts'/name for name in PUBLIC_SCRIPTS)
    paths.extend((root/'backend').glob('*.py'))
    paths.extend((root/'backend/collectors').glob('*.py'))
    for folder,suffix in [('migrations','.sql'),('prompts','.md'),('fixtures',None)]:
        paths.extend(p for p in (root/'backend'/folder).rglob('*') if p.is_file() and (suffix is None or p.suffix==suffix))
    paths.extend((root/'backend/tests').glob('test_*.py'))
    paths.extend(root/'backend/tests'/name for name in ['conftest.py','social_testkit.py'])
    paths.extend((root/'backend/tests').glob('frontend_*_contract.mjs'))
    paths.extend(p for p in (root/'frontend').rglob('*') if p.is_file() and p.suffix in ('.html','.css','.js','.svg'))
    return sorted(set(paths))

def build(root,output):
    destination=output/'finintel'
    if destination.exists(): raise SystemExit('Output already contains finintel; choose a new empty release directory')
    files=source_files(root)
    for path in files:
        if not path.is_file(): raise SystemExit('Missing required release file: '+str(path.relative_to(root)))
        relative=path.relative_to(root)
        if any(part in {'.local','.venv','outputs','node_modules','__pycache__'} for part in relative.parts):
            raise SystemExit('Private directory entered allowlist')
        if path.suffix.lower() in {'.db','.sqlite3','.enc','.log','.pdf','.docx'}:
            raise SystemExit('Private or generated file entered allowlist')
        # Reject private Windows user paths; application paths must be derived from config/home.
        if re.search(rb'[A-Za-z]:[\\/]Users[\\/][^\s"\'<>]+',path.read_bytes()):
            raise SystemExit('Machine-specific user path: '+str(relative))
    manifest=[]
    for path in files:
        relative=path.relative_to(root)
        target=destination/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,target)
        manifest.append({'path':relative.as_posix(),'bytes':target.stat().st_size,
            'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
    (destination/'release-manifest.json').write_text(json.dumps({'scope':'source-only','files':manifest},indent=2),encoding='utf-8')
    archive=output/'finintel-source.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(destination.rglob('*')):
            if path.is_file(): zipped.write(path,path.relative_to(output).as_posix())
    return {'file_count':len(files),'source_directory':str(destination),'archive':str(archive),
        'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    arguments=parser.parse_args()
    print(json.dumps(build(Path(__file__).resolve().parents[1],arguments.output.resolve()),indent=2))
