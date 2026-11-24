"""Update FLAC comments while copying every other metadata block and audio byte."""
import os
import shutil
import struct


def header(source):
    if source.read(4) != b"fLaC":
        raise ValueError("Writing music tags requires a native FLAC file.")
    blocks = []
    while True:
        entry = source.read(4)
        if len(entry) != 4:
            raise ValueError("The FLAC metadata header is incomplete.")
        kind = entry[0] & 127
        size = int.from_bytes(entry[1:], "big")
        blocks.append((kind, source.tell(), size))
        source.seek(size, os.SEEK_CUR)
        if entry[0] & 128:
            break
    if not blocks or blocks[0][0] != 0 or blocks[0][2] != 34:
        raise ValueError("The FLAC stream information is missing.")
    return blocks, source.tell()


def comments(source, blocks):
    entries = [block for block in blocks if block[0] == 4]
    if not entries:
        return b"Spliff", []
    if len(entries) != 1:
        raise ValueError("The FLAC has multiple comment blocks.")
    _, offset, size = entries[0]
    source.seek(offset)
    data = source.read(size)
    position = 0

    def integer():
        nonlocal position
        if position + 4 > len(data):
            raise ValueError("The FLAC comments are incomplete.")
        value, = struct.unpack_from("<I", data, position)
        position += 4
        return value

    def string():
        nonlocal position
        length = integer()
        end = position + length
        if end > len(data):
            raise ValueError("The FLAC comments are incomplete.")
        value = data[position:end]
        position = end
        return value

    vendor = string()
    count = integer()
    if count > (len(data) - position) // 4:
        raise ValueError("The FLAC comments are incomplete.")
    return vendor, [string() for _ in range(count)]


def music_tags(source):
    blocks, _ = header(source)
    _, entries = comments(source, blocks)
    return {key.decode("utf-8", "replace").upper(): value.decode("utf-8", "replace")
            for key, separator, value in (entry.partition(b"=") for entry in entries) if separator}


def write_tags(source, destination, tags):
    blocks, audio_offset = header(source)
    vendor, entries = comments(source, blocks)
    keys = {name.encode("ascii") for name in tags}
    entries = [entry for entry in entries if entry.partition(b"=")[0].upper() not in keys]
    entries.extend(f"{key}={value}".encode("utf-8") for key, value in tags.items())
    payload = (struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(entries))
               + b"".join(struct.pack("<I", len(entry)) + entry for entry in entries))
    if len(payload) >= 2**24:
        raise ValueError("The updated FLAC tags exceed the format limit.")
    blocks = [block for block in blocks if block[0] != 4]
    blocks.append((4, None, len(payload)))
    destination.write(b"fLaC")
    for index, (kind, offset, size) in enumerate(blocks):
        destination.write(bytes([kind | (128 if index == len(blocks) - 1 else 0)]) + size.to_bytes(3, "big"))
        if offset is None:
            destination.write(payload)
        else:
            source.seek(offset)
            block = source.read(size)
            if len(block) != size:
                raise ValueError("The FLAC metadata header is incomplete.")
            destination.write(block)
    source.seek(audio_offset)
    shutil.copyfileobj(source, destination, 1024 * 1024)
