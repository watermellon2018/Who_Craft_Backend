"""Validation for the 3D editor's parametric state.

The frontend zone registry (who_craft: character3d/zones.ts) is the source
of truth for which zones and parameters exist. The server intentionally does
NOT mirror that registry — a duplicated list would drift the first time the
editor gains a slider. Instead it enforces the structural contract the
engine relies on:

* the document is ``{zone_id: {param_id: value}}``;
* ids are short ``[A-Za-z0-9_]`` strings;
* leaf values are booleans, short strings (color hex / preset ids) or
  finite numbers — every numeric parameter the editor exposes lives in
  [-1, 1], so clamping to that interval is loss-free for valid clients;
* the document is bounded in size, so a hostile payload cannot grow the row.
"""

import math
import re

from w_craft_back.character_studio.services.errors import ValidationError

_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

MAX_ZONES = 120
MAX_PARAMS_PER_ZONE = 64
MAX_STRING_LEN = 64


def _validate_id(value, what):
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValidationError(f"Invalid {what} identifier.")
    return value


def validate_model3d_params(raw):
    """Return a cleaned copy of ``raw`` or raise ``ValidationError``."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("params must be an object of zones.")
    if len(raw) > MAX_ZONES:
        raise ValidationError("Too many zones.")

    cleaned = {}
    for zone_id, zone_params in raw.items():
        _validate_id(zone_id, "zone")
        if not isinstance(zone_params, dict):
            raise ValidationError(f"Zone '{zone_id}' must be an object of parameters.")
        if len(zone_params) > MAX_PARAMS_PER_ZONE:
            raise ValidationError(f"Too many parameters in zone '{zone_id}'.")

        cleaned_zone = {}
        for param_id, value in zone_params.items():
            _validate_id(param_id, "parameter")
            if isinstance(value, bool):
                cleaned_zone[param_id] = value
            elif isinstance(value, (int, float)):
                if not math.isfinite(value):
                    raise ValidationError(f"Parameter '{zone_id}.{param_id}' must be finite.")
                cleaned_zone[param_id] = max(-1.0, min(1.0, float(value)))
            elif isinstance(value, str):
                if len(value) > MAX_STRING_LEN:
                    raise ValidationError(f"Parameter '{zone_id}.{param_id}' string is too long.")
                cleaned_zone[param_id] = value
            else:
                raise ValidationError(
                    f"Parameter '{zone_id}.{param_id}' must be a number, string or boolean."
                )
        cleaned[zone_id] = cleaned_zone
    return cleaned
