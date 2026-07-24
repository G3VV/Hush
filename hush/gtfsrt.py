"""A minimal GTFS-Realtime reader.

The official reader needs the `protobuf` runtime plus generated bindings. Hush
has no third-party dependencies, and the slice of GTFS-RT we care about --
vehicle positions -- uses only three protobuf wire types, so it is decoded here
directly.

Wire format: each field is a varint key holding (field_number << 3 | wire_type),
followed by the payload. Wire types used: 0 varint, 1 64-bit, 2 length-
delimited, 5 32-bit.

Field numbers below come from gtfs-realtime.proto:

    FeedMessage.entity            = 2
    FeedEntity.id                 = 1
    FeedEntity.vehicle            = 4      (VehiclePosition)
    VehiclePosition.trip          = 1      (TripDescriptor)
    VehiclePosition.position      = 2      (Position)
    VehiclePosition.timestamp     = 5
    VehiclePosition.occupancy     = 9
    VehiclePosition.vehicle       = 8      (VehicleDescriptor)
    TripDescriptor.trip_id        = 1
    TripDescriptor.start_time     = 2
    TripDescriptor.start_date     = 3
    TripDescriptor.route_id       = 5
    Position.latitude             = 1      (float)
    Position.longitude            = 2      (float)
    Position.bearing              = 3      (float)
    Position.speed                = 5      (float)
    VehicleDescriptor.id          = 1
    VehicleDescriptor.label       = 2
"""

import struct


def _varint(buf, i):
    result = shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def walk(buf):
    """Yield (field_number, wire_type, value) for one protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            length, i = _varint(buf, i)
            value = buf[i:i + length]
            i += length
        elif wire == 5:
            value = buf[i:i + 4]
            i += 4
        elif wire == 1:
            value = buf[i:i + 8]
            i += 8
        else:
            # Groups (3/4) are deprecated and absent from GTFS-RT; anything
            # else means we have lost sync, so stop rather than emit rubbish.
            return
        yield field, wire, value


def _f32(raw):
    return struct.unpack("<f", raw)[0] if len(raw) == 4 else None


def _text(raw):
    return raw.decode("utf-8", "replace")


def _position(buf):
    lat = lon = bearing = speed = None
    for f, w, v in walk(buf):
        if w != 5:
            continue
        if f == 1:
            lat = _f32(v)
        elif f == 2:
            lon = _f32(v)
        elif f == 3:
            bearing = _f32(v)
        elif f == 5:
            speed = _f32(v)
    return lat, lon, bearing, speed


def _trip(buf):
    out = {}
    for f, w, v in walk(buf):
        if w != 2:
            continue
        if f == 1:
            out["trip_id"] = _text(v)
        elif f == 2:
            out["start_time"] = _text(v)
        elif f == 3:
            out["start_date"] = _text(v)
        elif f == 5:
            out["route_id"] = _text(v)
    return out


def _descriptor(buf):
    out = {}
    for f, w, v in walk(buf):
        if w != 2:
            continue
        if f == 1:
            out["vehicle_id"] = _text(v)
        elif f == 2:
            out["label"] = _text(v)
        elif f == 3:
            out["plate"] = _text(v)
    return out


def vehicle_positions(raw, bbox=None):
    """Decode a FeedMessage into vehicle dicts, optionally clipped to a bbox.

    bbox is (min_lat, min_lon, max_lat, max_lon).
    """
    out = []
    for f, w, v in walk(raw):
        if f != 2 or w != 2:
            continue                       # not a FeedEntity
        for ef, ew, ev in walk(v):
            if ef != 4 or ew != 2:
                continue                   # not a VehiclePosition
            rec = {"lat": None, "lon": None, "bearing": None, "speed": None,
                   "timestamp": None, "occupancy": None}
            for vf, vw, vv in walk(ev):
                if vf == 2 and vw == 2:
                    rec["lat"], rec["lon"], rec["bearing"], rec["speed"] = _position(vv)
                elif vf == 1 and vw == 2:
                    rec.update(_trip(vv))
                elif vf == 8 and vw == 2:
                    rec.update(_descriptor(vv))
                elif vf == 5 and vw == 0:
                    rec["timestamp"] = vv
                elif vf == 9 and vw == 0:
                    rec["occupancy"] = vv
            if rec["lat"] is None or rec["lon"] is None:
                continue
            if bbox:
                lo_la, lo_lo, hi_la, hi_lo = bbox
                if not (lo_la <= rec["lat"] <= hi_la and lo_lo <= rec["lon"] <= hi_lo):
                    continue
            out.append(rec)
    return out
