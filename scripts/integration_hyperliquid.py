"""Local delivery evidence; never overwrite existing destination data."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT.parent / 'MarketDataCSVBuilder'
EVIDENCE = ROOT / 'output/hyperliquid_acceptance'

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def inventory(root, paths):
    result = {}
    for relative in paths:
        path = root / relative
        for item in ([path] if path.is_file() else path.rglob('*')):
            if item.is_file():
                result[item.relative_to(root).as_posix()] = digest(item)
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['baseline', 'transfer', 'verify'])
    args = parser.parse_args()
    baseline = EVIDENCE / 'integration_baseline.json'
    if args.action == 'baseline':
        assert not baseline.exists(), 'Preserve the existing baseline'
        hashes = inventory(TARGET, ['output', 'data/cache', 'venv', 'config.toml',
            'run.bat', 'run_apx.bat', 'run_intraday.bat', 'setup.bat', 'requirements.txt'])
        baseline.write_text(json.dumps(hashes, indent=2), encoding='utf-8')
        print('Protected files:', len(hashes), flush=True)
    elif args.action == 'transfer':
        paths = ['output/intraday_hyperliquid/current', 'data/cache/intraday_hyperliquid']
        for name in paths:
            assert not (TARGET/name).exists(), f'Destination already exists: {name}'
        source = inventory(ROOT, paths)
        for name in paths:
            shutil.copytree(ROOT/name, TARGET/name)
        assert inventory(TARGET, paths) == source
        (EVIDENCE/'transfer.json').write_text(json.dumps(dict(files=len(source),
            bytes=sum((TARGET/p).stat().st_size for p in source), sha256_matches=True), indent=2))
        print('Transferred and SHA-256 verified:', len(source), flush=True)
    else:
        hashes = json.loads(baseline.read_text(encoding='utf-8'))
        changed = [p for p, value in hashes.items() if not (TARGET/p).is_file() or digest(TARGET/p) != value]
        result = dict(protected_files=len(hashes), changed=changed, preserved=not changed)
        (EVIDENCE/'preservation.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
        assert not changed

if __name__ == '__main__':
    main()
