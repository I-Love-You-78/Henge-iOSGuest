"""Dump the BuildIdentities of an IPSW or cloudOS archive without downloading it.

Same ZIP64 range-reading trick as ipsw_manifest.py, plus a per-identity table:
DeviceClass, ApBoardID, ApChipID, BuildStyle, Variant, and whether the image
carries the PCC identities vphone/Henge would impersonate (vresearch101ap,
vphone600ap).
"""
import json
import plistlib
import struct
import sys
import urllib.request
import zlib


def fetch(url, start, end):
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def head_size(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"]), dict(r.headers)


def central_directory(url, size):
    tail = fetch(url, max(0, size - 300_000), size - 1)
    loc = tail.rfind(b"PK\x06\x07")
    if loc >= 0:
        off = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
        z64 = fetch(url, off, off + 56)
        cd_size, cd_off = struct.unpack("<QQ", z64[40:56])
        return cd_off, cd_size
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise SystemExit("not a zip: no end-of-central-directory")
    cd_size, cd_off = struct.unpack("<II", tail[eocd + 12:eocd + 20])
    return cd_off, cd_size


def members(cd):
    out = []
    i = 0
    while True:
        i = cd.find(b"PK\x01\x02", i)
        if i < 0:
            return out
        method = struct.unpack("<H", cd[i + 10:i + 12])[0]
        csize = struct.unpack("<I", cd[i + 20:i + 24])[0]
        usize = struct.unpack("<I", cd[i + 24:i + 28])[0]
        nlen, elen, clen = struct.unpack("<HHH", cd[i + 28:i + 34])
        lho = struct.unpack("<I", cd[i + 42:i + 46])[0]
        name = cd[i + 46:i + 46 + nlen].decode("utf-8", "replace")
        extra = cd[i + 46 + nlen:i + 46 + nlen + elen]
        if 0xFFFFFFFF in (csize, usize, lho):
            j = 0
            while j + 4 <= len(extra):
                tag, ln = struct.unpack("<HH", extra[j:j + 4])
                if tag == 1:
                    vals = list(struct.unpack("<" + "Q" * (ln // 8), extra[j + 4:j + 4 + ln]))
                    if usize == 0xFFFFFFFF:
                        usize = vals.pop(0)
                    if csize == 0xFFFFFFFF:
                        csize = vals.pop(0)
                    if lho == 0xFFFFFFFF:
                        lho = vals.pop(0)
                    break
                j += 4 + ln
        out.append((name, method, csize, usize, lho))
        i += 46 + nlen + elen + clen


def read_member(url, method, csize, lho):
    header = fetch(url, lho, lho + 30)
    nlen, elen = struct.unpack("<HH", header[26:30])
    start = lho + 30 + nlen + elen
    raw = fetch(url, start, start + csize - 1)
    return raw if method == 0 else zlib.decompress(raw, -15)


def main(url, pattern="BuildManifest.plist"):
    size, headers = head_size(url)
    print(f"size: {size:,} bytes  content-type: {headers.get('Content-Type')}")
    magic = fetch(url, 0, 511)
    print(f"magic: {magic[:4].hex()}  {'zip' if magic[:2] == b'PK' else 'not a zip'}")
    if magic[:2] != b"PK":
        return
    cd_off, cd_size = central_directory(url, size)
    listing = members(fetch(url, cd_off, cd_off + cd_size - 1))
    names = [m[0] for m in listing]
    print(f"members: {len(names)}  e.g. {names[:4]}")
    hit = [m for m in listing if m[0].endswith(pattern)]
    if not hit:
        print(f"no {pattern}: look for {[n for n in names if 'Manifest' in n][:5]}")
        return
    name, method, csize, usize, lho = hit[0]
    plist = plistlib.loads(read_member(url, method, csize, lho))
    print(f"\n{name}: macOS/iOS {plist.get('ProductVersion')} build {plist.get('ProductBuildVersion')}")
    types = plist.get("SupportedProductTypes", [])
    print(f"SupportedProductTypes ({len(types)}): {', '.join(types[:10])}{' ...' if len(types) > 10 else ''}")
    ids = plist.get("BuildIdentities", [])
    print(f"BuildIdentities: {len(ids)}")
    classes = {}
    for ident in ids:
        info = ident.get("Info", {})
        cls = info.get("DeviceClass", "?")
        classes[cls] = classes.get(cls, 0) + 1
        if classes[cls] > 1:
            continue
        print(f"  - {cls:16s} board={info.get('ApBoardID')} chip={info.get('ApChipID')} "
              f"style={info.get('BuildStyle')} variant={ident.get('Variant')} "
              f"product={info.get('ProductType', info.get('Ap,ProductType'))}")
    for want in ("vresearch101ap", "vphone600ap"):
        print(f"{want}: {'YES' if want in classes else 'no'}")
    print(json.dumps({"identities": sorted(classes)}, ensure_ascii=False))


if __name__ == "__main__":
    for u in sys.argv[1:]:
        print("=" * 70)
        print(u)
        try:
            main(u)
        except Exception as e:
            print(f"failed: {type(e).__name__}: {e}")
