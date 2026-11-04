"""Download the pinned Gradient artifacts, or verify an existing model folder."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
FILES = ('model.onnx', 'tokenizer.json', 'config.json')
PREFIX = 'https://huggingface.co/batmac/gradient-ai-text-detector-onnx/resolve/776d0164ba0de631036c70f6a146d68a7cd4ea4e/'

def matches(path, metadata):
    if not path.is_file() or path.stat().st_size != metadata['size']:
        return False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest() == metadata['sha256']

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify only; never download.')
    parser.add_argument('--model-dir', type=Path, default=ROOT/'var/ai_detector')
    args = parser.parse_args()
    manifest = json.loads((Path(__file__).resolve().parent/'manifest.json').read_text(encoding='utf-8'))
    for name in FILES:
        metadata = manifest['files'][name]
        if not metadata['source'].startswith(PREFIX):
            raise ValueError('Unexpected model source; refusing download.')
        target = args.model_dir/name
        if matches(target, metadata):
            print('Verified:', name, flush=True)
            continue
        if args.check:
            raise SystemExit('Missing or incorrect model file: '+str(target))
        args.model_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix+'.part')
        print(f'Downloading {name} ({metadata["size"]/1_000_000:.1f} MB)...', flush=True)
        try:
            with urllib.request.urlopen(metadata['source'], timeout=60) as response, temporary.open('wb') as output:
                for block in iter(lambda: response.read(1024 * 1024), b''):
                    output.write(block)
            if not matches(temporary, metadata):
                raise ValueError('Downloaded file failed integrity check: '+name)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    if not args.check:
        (args.model_dir/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Packaged Gradient model is ready for the portal worker.')

if __name__ == '__main__':
    main()
