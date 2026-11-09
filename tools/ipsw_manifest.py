"""Read one member out of a huge remote ZIP (ZIP64) with HTTP range requests.

Used to look at Apple's macOS restore images without downloading 16-20 GB: the
question is only what their BuildManifest.plist says, and whether it still names
a Virtual Mac (VirtualMac2,1) — which is what Henge's restore chain boots.
"""
import io
import plistlib
import struct
import sys
import urllib.request
import zlib

SIG_EOCD = b"PK\x05\x06"
SIG_ZIP64_EOCD = b"PK\x06\x06"
SIG_ZIP64_LOC = b"PK\x06\x07"


def fetch(url, start, end):
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def size_of(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"])


def find_central_directory(url, size):
    tail = fetch(url, max(0, size - 200_000), size - 1)
    loc = tail.rfind(SIG_ZIP64_LOC)
    if loc >= 0:
        z64_off = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
        z64 = fetch(url, z64_off, z64_off + 56)
        cd_size, cd_off = struct.unpack("<QQ", z64[40:56])
        return cd_off, cd_size
    eocd = tail.rfind(SIG_EOCD)
    if eocd < 0:
        raise SystemExit("no end-of-central-directory found")
    cd_size, cd_off = struct.unpack("<II", tail[eocd + 12:eocd + 20])
    return cd_off, cd_size


def find_member(cd, name):
    want = name.encode()
    i = 0
    while True:
        i = cd.find(b"PK\x01\x02", i)
        if i < 0:
            return None
        (method, csize, usize, nlen, elen, clen) = (
            struct.unpack("<H", cd[i + 10:i + 12])[0],
            struct.unpack("<I", cd[i + 20:i + 24])[0],
            struct.unpack("<I", cd[i + 24:i + 28])[0],
            struct.unpack("<H", cd[i + 28:i + 30])[0],
            struct.unpack("<H", cd[i + 30:i + 32])[0],
            struct.unpack("<H", cd[i + 32:i + 34])[0],
        )
        lho = struct.unpack("<I", cd[i + 42:i + 46])[0]
        fname = cd[i + 46:i + 46 + nlen]
        extra = cd[i + 46 + nlen:i + 46 + nlen + elen]
        if fname == want:
            # ZIP64 extended information fixes up the 32-bit fields when they are 0xffffffff
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
            return method, csize, usize, lho
        i += 46 + nlen + elen + clen
    return None


def read_member(url, method, csize, lho):
    header = fetch(url, lho, lho + 30)
    nlen, elen = struct.unpack("<HH", header[26:30])
    data_start = lho + 30 + nlen + elen
    raw = fetch(url, data_start, data_start + csize - 1)
    if method == 0:
        return raw
    if method == 8:
        return zlib.decompress(raw, -15)
    raise SystemExit(f"compression method {method} not handled")


def report(url, label):
    size = size_of(url)
    cd_off, cd_size = find_central_directory(url, size)
    cd = fetch(url, cd_off, cd_off + cd_size - 1)
    member = find_member(cd, "BuildManifest.plist")
    if not member:
        print(f"{label}: BuildManifest.plist not found")
        return
    method, csize, usize, lho = member
    data = read_member(url, method, csize, lho)
    plist = plistlib.loads(data)
    types = plist.get("SupportedProductTypes", [])
    vm = [t for t in types if "Virtual" in t or "vm" in t.lower()]
    print(f"{label}: macOS {plist.get('ProductVersion')} build {plist.get('ProductBuildVersion')}")
    print(f"   product types ({len(types)}): {', '.join(types[:8])}{' ...' if len(types) > 8 else ''}")
    print(f"   virtual Mac entry: {'YES ' + str(vm) if vm else 'no'}")


if __name__ == "__main__":
    for url, label in [
        ("https://updates.cdn-apple.com/2025SummerFCS/fullrestores/093-10809/CFD6DD38-DAF0-40DA-854F-31AAD1294C6F/UniversalMac_15.6.1_24G90_Restore.ipsw", "15.6.1"),
        ("https://updates.cdn-apple.com/2026SummerFCS/fullrestores/140-75212/A2A24B94-1FC1-45A3-93F7-C51B02AF1F4D/UniversalMac_26.6.2_25G83_Restore.ipsw", "26.6.2"),
        ("https://updates.cdn-apple.com/2026FallFCS/c9671975-52db-4fb5-9af2-00217169ba8d/UniversalMac_27.2_26B123_Restore.ipsw", "27.2 (listed)"),
    ]:
        try:
            report(url, label)
        except Exception as e:
            print(f"{label}: failed: {e}")
