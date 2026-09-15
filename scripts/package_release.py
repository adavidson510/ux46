"""Reproducible source bundle from the Git index; never package local state.

install.sh is distributed separately so its archive checksum is not circular.
"""
import argparse
import gzip
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def package(destination):
    destination = Path(destination).resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError('Write release artifacts outside the source tree')
    entries = subprocess.check_output(['git','ls-files','--stage','-z'],cwd=ROOT).split(b'\0')
    with destination.open('xb') as output:
        with gzip.GzipFile(filename='',mode='wb',fileobj=output,mtime=0) as compressed:
            with tarfile.open(fileobj=compressed,mode='w') as archive:
                for entry in sorted(filter(None, entries)):
                    metadata, filename = entry.split(b'\t',1)
                    mode, oid, stage = metadata.decode().split()
                    name = filename.decode()
                    if name == 'install.sh': continue
                    if stage != '0' or mode not in ('100644','100755'):
                        raise ValueError('Only resolved regular source files may be packaged')
                    data = subprocess.check_output(['git','cat-file','blob',oid],cwd=ROOT)
                    item = tarfile.TarInfo(name)
                    item.size = len(data); item.mode = 0o755 if mode == '100755' else 0o644
                    archive.addfile(item,io.BytesIO(data))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',type=Path)
    print(package(parser.parse_args().destination))
