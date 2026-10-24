"""Minimal protobuf wire-format encoder/decoder.

Messages are described by plain dicts (see protocol.py):

    {field_number: (name, kind, extra)}

kind is one of: "uint", "sint", "bool", "string", "bytes", "enum", "msg"
(plus a leading "*" for repeated fields, e.g. "*msg"). extra is the nested message spec
for "msg", the enum dict {name: number} for "enum", and None otherwise.

Decoded messages are dicts keyed by field name. Enum values decode to their name (or the
number, if unknown). Fields with default values are simply absent, as in proto3.
"""

VARINT, I64, LEN, I32 = 0, 1, 2, 5


def _varint(value):
    out = bytearray()
    value &= (1 << 64) - 1
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf, i):
    result = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _zigzag(n):
    return (n << 1) ^ (n >> 63)


def _unzigzag(n):
    return (n >> 1) ^ -(n & 1)


def _key(num, wtype):
    return _varint((num << 3) | wtype)


def encode(spec, values):
    """Encode dict `values` according to `spec`. Unknown keys raise KeyError."""
    by_name = {name: (num, kind, extra) for num, (name, kind, extra) in spec.items()}
    out = bytearray()
    for name, value in values.items():
        if value is None:
            continue
        if name == "_unknown":  # fields we don't model: re-emit unchanged
            for num, wtype, raw in value:
                out += _key(num, wtype)
                if wtype == VARINT:
                    out += _varint(raw)
                elif wtype == LEN:
                    out += _varint(len(raw)) + raw
                elif wtype == I32:
                    out += raw.to_bytes(4, "little")
                else:
                    out += raw.to_bytes(8, "little")
            continue
        num, kind, extra = by_name[name]
        repeated = kind.startswith("*")
        kind = kind.lstrip("*")
        for v in (value if repeated else [value]):
            out += _encode_field(num, kind, extra, v)
    return bytes(out)


def _encode_field(num, kind, extra, v):
    if kind == "uint":
        return _key(num, VARINT) + _varint(int(v))
    if kind == "sint":
        return _key(num, VARINT) + _varint(_zigzag(int(v)))
    if kind == "bool":
        return _key(num, VARINT) + _varint(1 if v else 0)
    if kind == "enum":
        n = extra[v] if isinstance(v, str) else int(v)
        return _key(num, VARINT) + _varint(n)
    if kind in ("string", "bytes"):
        data = v.encode("utf-8") if isinstance(v, str) else bytes(v)
        return _key(num, LEN) + _varint(len(data)) + data
    if kind == "msg":
        data = encode(extra, v)
        return _key(num, LEN) + _varint(len(data)) + data
    raise ValueError("unknown kind %r" % kind)


def decode(spec, buf):
    """Decode bytes into a dict according to `spec`.

    Fields not in the spec are kept under '_unknown' as (number, wire_type, raw) and are
    re-emitted unchanged by encode(), so configs read from the device round-trip intact."""
    out = {}
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        num, wtype = key >> 3, key & 7
        if wtype == VARINT:
            raw, i = _read_varint(buf, i)
        elif wtype == LEN:
            n, i = _read_varint(buf, i)
            raw, i = buf[i:i + n], i + n
            if i > len(buf):
                raise ValueError("truncated field %d" % num)
        elif wtype == I32:
            raw, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        elif wtype == I64:
            raw, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        else:
            raise ValueError("unsupported wire type %d" % wtype)

        if num not in spec:
            out.setdefault("_unknown", []).append((num, wtype, bytes(raw) if wtype == LEN else raw))
            continue
        name, kind, extra = spec[num]
        repeated = kind.startswith("*")
        kind = kind.lstrip("*")
        if kind == "uint":
            v = raw
        elif kind == "sint":
            v = _unzigzag(raw)
        elif kind == "bool":
            v = bool(raw)
        elif kind == "enum":
            names = {n: k for k, n in extra.items()}
            v = names.get(raw, raw)
        elif kind == "string":
            v = bytes(raw).decode("utf-8", "replace")
        elif kind == "bytes":
            v = bytes(raw)
        elif kind == "msg":
            v = decode(extra, raw)
        else:
            raise ValueError("unknown kind %r" % kind)
        if repeated:
            out.setdefault(name, []).append(v)
        else:
            out[name] = v
    return out
