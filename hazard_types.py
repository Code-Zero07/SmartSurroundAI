"""hazard_types.py
------------------
Shared hazard-type vocabulary (no heavy imports so db.py migrations can use
it without pulling in the ML stack). Maps detector class names to normalized
slugs and humanizes labels for display/email/PDF.
"""

DAMAGE_TYPE_SLUGS = {
    "Potholes": "pothole",
    "Alligator Crack": "alligator_crack",
    "Longitudinal Crack": "longitudinal_crack",
    "Transverse Crack": "transverse_crack",
    "Unclassified damage": "unclassified_damage",
}

# The detector's "unclassified damage" tier, named once here so callers do not
# repeat either spelling. detector.analyze_road emits it with damage_type=null
# and accepted=False by design: its confidence is combined evidence from
# several weak classes rather than one named class's score.
UNCLASSIFIED_LABEL = "Unclassified damage"
UNCLASSIFIED_SLUG = DAMAGE_TYPE_SLUGS[UNCLASSIFIED_LABEL]


def type_slug(damage_class):
    """Normalized hazard type for a detector class name, or None."""
    return DAMAGE_TYPE_SLUGS.get(damage_class)


def humanize_label(value):
    """'road_damage' -> 'Road Damage'; 'waterlogged_road' -> 'Waterlogged Road';
    None/'' -> 'Unspecified'."""
    if not value:
        return "Unspecified"
    return " ".join(word.capitalize() for word in str(value).replace("-", " ").split("_"))