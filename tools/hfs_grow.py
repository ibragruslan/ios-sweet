#!/usr/bin/env python3
"""Non-destructively grow HFS+ filesystem on nabu userdata (/dev/block/sda33).

Expands the filesystem from 12.5 GB to ~40.35 GB (+27.8 GB free space)
without re-flashing or moving any existing files.
"""
import argparse, struct, subprocess, sys

BLOCK_SIZE = 4096
TOTAL_DEV_BYTES = 40363835392 # /dev/block/sda33
XNU_LOG_BLOCKS = 1025

def adb_read(offset_bytes, length_bytes):
    bs = 4096
    skip = offset_bytes // bs
    count = (length_bytes + bs - 1) // bs
    cmd = f"dd if=/dev/block/sda33 bs={bs} skip={skip} count={count} 2>/dev/null"
    p = subprocess.run(["adb", "exec-out", cmd], capture_output=True, check=True)
    rem = offset_bytes % bs
    return p.stdout[rem : rem + length_bytes]

def adb_write(offset_bytes, data):
    bs = 4096
    assert offset_bytes % bs == 0, "offset must be block-aligned"
    assert len(data) % bs == 0, "data length must be block-aligned"
    skip = offset_bytes // bs
    count = len(data) // bs
    print(f"[*] Writing {len(data)} bytes ({count} blocks) to /dev/block/sda33 at block {skip}...")
    proc = subprocess.Popen(
        ["adb", "shell", f"dd of=/dev/block/sda33 bs={bs} seek={skip} count={count} conv=notrunc 2>/dev/null"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = proc.communicate(input=data)
    if proc.returncode != 0:
        raise RuntimeError(f"dd write failed: {stderr.decode()}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually write changes to device")
    args = parser.parse_args()

    print("[*] Reading HFS+ Volume Header from /dev/block/sda33...")
    raw_block = bytearray(adb_read(0, BLOCK_SIZE))
    vh_bytes = raw_block[1024 : 1024 + 512]
    sig, ver = struct.unpack_from('>2sH', vh_bytes, 0)
    assert sig in (b'HX', b'H+'), f"Invalid HFS signature: {sig}"
    
    blk_size, total_blocks, free_blocks = struct.unpack_from('>III', vh_bytes, 40)
    print(f"Current HFS+: BlockSize={blk_size}, TotalBlocks={total_blocks} ({total_blocks * blk_size / 1e9:.2f} GB), FreeBlocks={free_blocks} ({free_blocks * blk_size / 1e6:.2f} MB)")
    
    alloc_fork = vh_bytes[112 : 112 + 80]
    alloc_log_size, alloc_clump, alloc_tot_blks = struct.unpack_from('>QII', alloc_fork, 0)
    ext0_start, ext0_count = struct.unpack_from('>II', alloc_fork, 16)
    ext1_start, ext1_count = struct.unpack_from('>II', alloc_fork, 24)
    print(f"AllocationFile: logicalSize={alloc_log_size}, blocks={alloc_tot_blks}")
    print(f"  Extent 0: startBlock={ext0_start}, blockCount={ext0_count}")
    print(f"  Extent 1: startBlock={ext1_start}, blockCount={ext1_count}")
    
    new_total_blocks = (TOTAL_DEV_BYTES // BLOCK_SIZE) - XNU_LOG_BLOCKS
    print(f"\n[*] Target geometry:")
    print(f"  New TotalBlocks: {new_total_blocks} ({new_total_blocks * BLOCK_SIZE / 1e9:.2f} GB)")
    
    new_alloc_size = (new_total_blocks + 7) // 8
    new_alloc_blocks = (new_alloc_size + BLOCK_SIZE - 1) // BLOCK_SIZE
    needed_ext1_blocks = new_alloc_blocks - ext0_count
    ext1_start_block = total_blocks
    
    print(f"  New AllocationFile size: {new_alloc_size} bytes ({new_alloc_blocks} blocks)")
    print(f"  Extent 1: startBlock={ext1_start_block}, blockCount={needed_ext1_blocks} blocks")
    
    added_blocks = (new_total_blocks - total_blocks) - needed_ext1_blocks
    new_free_blocks = free_blocks + added_blocks
    print(f"  New FreeBlocks: {new_free_blocks} ({new_free_blocks * BLOCK_SIZE / 1e9:.2f} GB FREE SPACE!)")

    if not args.apply:
        print("\n[!] Dry run complete. Run with --apply to commit expansion.")
        return

    # 1. Backup original volume header
    open("backup_hfs_vh.bin", "wb").write(raw_block)
    print("[+] Saved backup of block 0 to backup_hfs_vh.bin")

    # 2. Build Extent 1 blocks for AllocationFile
    # In HFS+ allocation bitmap:
    # 1 bit per block: 1 = allocated, 0 = free.
    # Extent 1 covers blocks from total_blocks up to new_total_blocks - 1.
    # The first needed_ext1_blocks represent Extent 1 itself, so they must be marked allocated (1).
    ext1_bytes = bytearray(needed_ext1_blocks * BLOCK_SIZE)
    # Mark needed_ext1_blocks bits as 1
    full_bytes = needed_ext1_blocks // 8
    rem_bits = needed_ext1_blocks % 8
    for i in range(full_bytes):
        ext1_bytes[i] = 0xFF
    if rem_bits:
        # High bits set
        ext1_bytes[full_bytes] = (0xFF << (8 - rem_bits)) & 0xFF

    # Mark bits past new_total_blocks in the last byte as 1 (HFS+ standard: pad bits are 1s)
    # The bit for block (new_total_blocks - 1) is at index:
    total_alloc_bits = new_total_blocks
    # Bits within Extent 1 start at offset (ext0_count * BLOCK_SIZE * 8)
    ext0_bits = ext0_count * BLOCK_SIZE * 8
    ext1_bits = total_alloc_bits - ext0_bits
    last_byte_idx = (ext1_bits - 1) // 8
    valid_bits_in_last_byte = ext1_bits % 8
    if valid_bits_in_last_byte != 0:
        # Lower bits past valid_bits are 1
        pad_mask = (1 << (8 - valid_bits_in_last_byte)) - 1
        ext1_bytes[last_byte_idx] |= pad_mask
    
    # Bytes from new_alloc_size to the end of ext1_bytes are 0
    print(f"[*] Writing AllocationFile Extent 1 ({len(ext1_bytes)} bytes) to block {ext1_start_block}...")
    adb_write(ext1_start_block * BLOCK_SIZE, bytes(ext1_bytes))

    # 2.5 Clear journal header magic to prevent stale journal replay from rolling back Volume Header
    print("[*] Clearing journal header magic at block 96 to prevent rollback...")
    zero_jhdr = bytearray(adb_read(96 * BLOCK_SIZE, BLOCK_SIZE))
    zero_jhdr[0:8] = b'\x00' * 8
    adb_write(96 * BLOCK_SIZE, bytes(zero_jhdr))

    # 3. Update Volume Header
    struct.pack_into('>II', vh_bytes, 40 + 4, new_total_blocks, new_free_blocks)
    struct.pack_into('>QII', vh_bytes, 112, new_alloc_size, alloc_clump, new_alloc_blocks)
    struct.pack_into('>II', vh_bytes, 112 + 24, ext1_start_block, needed_ext1_blocks)
    # Clear kHFSVolumeJournaledBit (bit 13) in attributes (offset 4) so HFS mounts without replay
    attrs, = struct.unpack_from('>I', vh_bytes, 4)
    attrs &= ~(1 << 13)
    struct.pack_into('>I', vh_bytes, 4, attrs)
    
    raw_block[1024 : 1024 + 512] = vh_bytes

    # 4. Write primary Volume Header (block 0)
    print("[*] Writing updated primary Volume Header to block 0...")
    adb_write(0, bytes(raw_block))

    # 5. Write alternate Volume Header at end of new volume
    # Alternate VH is at: new_total_blocks * BLOCK_SIZE - 1024
    alt_block_num = new_total_blocks - 1
    print(f"[*] Reading block {alt_block_num} for alternate Volume Header...")
    alt_raw_block = bytearray(adb_read(alt_block_num * BLOCK_SIZE, BLOCK_SIZE))
    alt_raw_block[BLOCK_SIZE - 1024 : BLOCK_SIZE - 1024 + 512] = vh_bytes
    print(f"[*] Writing alternate Volume Header to block {alt_block_num}...")
    adb_write(alt_block_num * BLOCK_SIZE, bytes(alt_raw_block))

    # 6. Zero out old alternate Volume Header at old_total_blocks * BLOCK_SIZE - 1024
    old_alt_block_num = total_blocks - 1
    print(f"[*] Clearing old alternate Volume Header at block {old_alt_block_num}...")
    old_alt_raw = bytearray(adb_read(old_alt_block_num * BLOCK_SIZE, BLOCK_SIZE))
    old_alt_raw[BLOCK_SIZE - 1024 : BLOCK_SIZE - 1024 + 512] = b'\x00' * 512
    adb_write(old_alt_block_num * BLOCK_SIZE, bytes(old_alt_raw))

    print("\n[✓] SUCCESS! HFS+ partition expanded to 40.36 GB.")
    print(f"[✓] Free space increased from 61.75 MB to {new_free_blocks * BLOCK_SIZE / 1e9:.2f} GB!")

if __name__ == '__main__':
    main()
