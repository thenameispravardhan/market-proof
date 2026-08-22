"""Recover the tbt_ws protobuf schema from the shipped descriptor.

c++.text §7.6 — "ACTION BEFORE PHASE 13 — do this in week one, it costs one
day... One day of checking can save three months of work and the largest risk
in the project."

fyers-apiv3 ships a third socket (`tbt_ws.py`, wss://rtsocket-api.fyers.in/versova)
that speaks PROTOBUF rather than the hand-rolled HSM binary format the data
socket uses. Protobuf is self-describing and the C++ bindings are GENERATED —
no stateful delta accumulation, no positional field ordering, no scale-factor
guessing. If that feed carries what the strategy needs, §9 PHASE 13 shrinks
from 16 weeks of reverse-engineering to 8 weeks of codegen.

This script does the offline half of §7.6 — it needs no entitlement, no market
session and no network:

    python scripts/recover_tbt_proto.py --out cpp/proto/

  1. dumps the FileDescriptorProto shipped inside msg_pb2
  2. reconstructs a readable msg.proto from it
  3. reports every message/field so the shape can be compared against §7.6

The ONLINE half (is the account entitled? does it carry LTP/bid/ask for NSE
CASH equities? how does its latency compare?) needs a live session — see
scripts/probe_tbt_entitlement.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TYPE_NAMES = {
    1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32",
    6: "fixed64", 7: "fixed32", 8: "bool", 9: "string", 10: "group",
    11: "message", 12: "bytes", 13: "uint32", 14: "enum", 15: "sfixed32",
    16: "sfixed64", 17: "sint32", 18: "sint64",
}
LABELS = {1: "optional", 2: "required", 3: "repeated"}


def render(fdp) -> str:
    """Reconstruct .proto source from a FileDescriptorProto."""
    out: list[str] = []
    syntax = fdp.syntax or "proto2"
    out.append(f'syntax = "{syntax}";')
    if fdp.package:
        out.append(f"package {fdp.package};")
    out.append("")
    out.append("// Reconstructed by scripts/recover_tbt_proto.py from the")
    out.append("// FileDescriptorProto shipped inside fyers_apiv3.FyersWebsocket.msg_pb2.")
    out.append("// Source of truth is the descriptor, not this rendering.")
    out.append("")
    # Every scalar in this schema is a google.protobuf wrapper type, so the
    # import is mandatory -- without it protoc rejects the file and the whole
    # point of §7.6 (generated bindings, not hand-written decoding) is lost.
    for dep in fdp.dependency:
        out.append(f'import "{dep}";')
    if fdp.dependency:
        out.append("")

    def render_msg(m, indent: int = 0) -> None:
        pad = "  " * indent
        # A protobuf map<k,v> is sugar for a nested entry message; skip those
        # synthetic types and let the map field itself carry the shape.
        if m.options.map_entry:
            return
        out.append(f"{pad}message {m.name} {{")
        maps = {
            n.name.lower().replace("entry", ""): n
            for n in m.nested_type
            if n.options.map_entry
        }
        for f in m.field:
            tname = TYPE_NAMES.get(f.type, f"type{f.type}")
            if f.type in (11, 14):  # message / enum -> use the type name
                tname = f.type_name.lstrip(".")
                short = tname.rsplit(".", 1)[-1]
                # google.protobuf.Int64Value etc. MUST stay qualified.
                external = tname.startswith("google.protobuf.")
                entry = maps.get(short.lower().replace("entry", ""))
                if entry is not None and f.label == 3:
                    kt = TYPE_NAMES.get(entry.field[0].type, "string")
                    vt = entry.field[1].type_name.lstrip(".").rsplit(".", 1)[-1] \
                        if entry.field[1].type == 11 \
                        else TYPE_NAMES.get(entry.field[1].type, "string")
                    out.append(f"{pad}  map<{kt}, {vt}> {f.name} = {f.number};")
                    continue
                tname = tname if external else short
            label = "" if f.label == 1 and syntax == "proto3" else LABELS.get(f.label, "") + " "
            if f.label == 3:
                label = "repeated "
            out.append(f"{pad}  {label}{tname} {f.name} = {f.number};")
        for n in m.nested_type:
            render_msg(n, indent + 1)
        for e in m.enum_type:
            out.append(f"{pad}  enum {e.name} {{")
            for v in e.value:
                out.append(f"{pad}    {v.name} = {v.number};")
            out.append(f"{pad}  }}")
        out.append(f"{pad}}}")
        out.append("")

    for m in fdp.message_type:
        render_msg(m)
    for e in fdp.enum_type:
        out.append(f"enum {e.name} {{")
        for v in e.value:
            out.append(f"  {v.name} = {v.number};")
        out.append("}")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("cpp/proto"))
    args = ap.parse_args()

    try:
        from fyers_apiv3.FyersWebsocket import msg_pb2
        from google.protobuf import descriptor_pb2
    except ImportError as e:
        print(f"fyers_apiv3 / protobuf not importable here: {e}", file=sys.stderr)
        print("Run this on the server, where the trading venv lives.", file=sys.stderr)
        return 2

    fdp = descriptor_pb2.FileDescriptorProto()
    msg_pb2.DESCRIPTOR.CopyToProto(fdp)

    args.out.mkdir(parents=True, exist_ok=True)
    blob = args.out / "msg_descriptor.bin"
    blob.write_bytes(fdp.SerializeToString())
    proto = args.out / "msg.proto"
    proto.write_text(render(fdp), encoding="utf-8")

    print(f"descriptor  {blob}  ({blob.stat().st_size} bytes)")
    print(f"proto       {proto}")
    print(f"\npackage     {fdp.package or '(none)'}")
    print(f"syntax      {fdp.syntax or 'proto2'}")
    print(f"messages    {len(fdp.message_type)}\n")

    # §7.6 lists the shape it expects; print the real one so they can be
    # compared field by field rather than trusted.
    for m in fdp.message_type:
        if m.options.map_entry:
            continue
        fields = ", ".join(f.name for f in m.field)
        print(f"  {m.name}({fields})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
