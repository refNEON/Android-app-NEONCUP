#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
检查 APK / AAB（zip）里的 .so 是否满足 Android 16KB 页对齐要求。

纯标准库实现，不需要 NDK、不需要 readelf，Windows 也能直接跑。

用法：
    python check_16kb.py path/to/app-debug.apk

检查两层，缺一层在 16KB 页设备上都会 dlopen 失败：
  1. ELF 层：每个 PT_LOAD 段的 p_align >= 16384
             且 p_offset % 16384 == p_vaddr % 16384
  2. ZIP 层：未压缩（STORED）存储的 .so，数据在包内的偏移 % 16384 == 0
             （压缩存储时这一层不适用，安装器会解压后加载）

退出码：0 = 全部通过；1 = 存在未对齐项。
"""

import os
import struct
import sys
import zipfile

PAGE = 16384  # 16 KB


def elf_unaligned_loads(data):
    """返回该 ELF 中所有不合规 PT_LOAD 段的 (offset, vaddr, align) 列表。"""
    if data[:4] != b"\x7fELF":
        return ["not an ELF file"]
    is64 = data[4] == 2

    if is64:
        e_phoff, = struct.unpack_from("<Q", data, 0x20)
        e_phentsize, e_phnum = struct.unpack_from("<HH", data, 0x36)
    else:
        e_phoff, = struct.unpack_from("<I", data, 0x1C)
        e_phentsize, e_phnum = struct.unpack_from("<HH", data, 0x2A)

    bad = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if is64:
            (p_type, _flags, p_offset, p_vaddr,
             _paddr, _filesz, _memsz, p_align) = struct.unpack_from("<IIQQQQQQ", data, off)
        else:
            (p_type, p_offset, p_vaddr, _paddr,
             _filesz, _memsz, _flags, p_align) = struct.unpack_from("<IIIIIIII", data, off)

        if p_type != 1:  # 只看 PT_LOAD
            continue
        if p_align < PAGE or (p_offset % PAGE) != (p_vaddr % PAGE):
            bad.append((hex(p_offset), hex(p_vaddr), hex(p_align)))
    return bad


def local_header_data_offset(fp, info):
    """算 .so 数据在 zip 里的绝对偏移：必须用「本地头」里的 nlen/elen，
    不能用 ZipInfo.extra（那是中央目录的 extra，长度可能不同）。"""
    fp.seek(info.header_offset)
    header = fp.read(30)
    nlen, elen = struct.unpack_from("<HH", header, 26)
    return info.header_offset + 30 + nlen + elen


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    apk = sys.argv[1]
    if not os.path.isfile(apk):
        print("找不到文件:", apk)
        return 2

    zf = zipfile.ZipFile(apk)
    with open(apk, "rb") as fp:
        sos = [i for i in zf.infolist()
               if i.filename.startswith("lib/") and i.filename.endswith(".so")]

        bad_zip, bad_elf = [], []
        for info in sos:
            if info.compress_type == 0:  # STORED：必须 16KB 对齐
                off = local_header_data_offset(fp, info)
                if off % PAGE != 0:
                    bad_zip.append((info.filename, off, off % PAGE))
            unaligned = elf_unaligned_loads(zf.read(info.filename))
            if unaligned:
                bad_elf.append((info.filename, unaligned))

    print("文件:", os.path.basename(apk))
    print("lib/*.so 数量:", len(sos),
          " 压缩方式:", {i.compress_type for i in sos} or "-", "(0=STORED 不压缩)")
    print()

    ok = True
    if bad_zip:
        ok = False
        print("❌ ZIP 内未按 16KB 偏移:")
        for name, off, rem in bad_zip:
            print(f"   {name}  offset={off}  offset%16384={rem}")
    else:
        print("✅ ZIP 对齐: 通过")

    if bad_elf:
        ok = False
        print("❌ ELF LOAD 段未 16KB 对齐:")
        for name, items in bad_elf:
            print(f"   {name}  {items}")
    else:
        print("✅ ELF 对齐: 通过")

    print()
    print("结论:", "16KB 合规 ✅" if ok else "不合规，需要处理 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
