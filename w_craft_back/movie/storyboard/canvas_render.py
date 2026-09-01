"""Render clean composition conditions without editor UI, annotations or arrows."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageOps


def render_canvas(document: dict) -> bytes:
    """Use normalized camera-view geometry and ordered layers, never remote SVG."""
    dimensions = {"16:9": (1024, 576), "9:16": (576, 1024), "1:1": (768, 768)}
    width, height = dimensions[document["aspectRatio"]]
    scene = Image.new("RGBA", (width, height), "#f5f3ed")
    for item in document["objects"]:
        if item["hidden"]:
            continue
        w = max(2, round(width * item["width"] / 100))
        h = max(2, round(height * item["height"] / 100))
        shape = Image.new("RGBA", (w, h))
        draw = ImageDraw.Draw(shape)
        color, fill = "#374151", "#c5cbd3"
        stroke = max(2, round(min(w, h) * 0.045))
        if item["kind"] == "person":
            head = (w * .34, 0, w * .66, h * .2)
            draw.ellipse(head, fill=fill, outline=color, width=stroke)
            draw.line([(w * .5, h * .2), (w * .5, h * .65)], fill=color, width=stroke)
            if item["pose"] == "profile":
                draw.polygon([(w * .62, h * .06), (w * .77, h * .12),
                              (w * .62, h * .15)], fill=color)
            elif item["pose"] == "front":
                draw.ellipse((w * .42, h * .07, w * .46, h * .09), fill=color)
                draw.ellipse((w * .54, h * .07, w * .58, h * .09), fill=color)
            draw.line([(w * .15, h * .5), (w * .5, h * .3),
                       (w * .85, h * .5)], fill=color, width=stroke)
            if item["pose"] == "sitting":
                draw.line([(w * .5, h * .65), (w * .85, h * .65),
                           (w * .85, h * .96)], fill=color, width=stroke)
            else:
                draw.line([(w * .15, h * .97), (w * .5, h * .65),
                           (w * .85, h * .97)], fill=color, width=stroke)
        elif item["kind"] == "animal":
            draw.ellipse((w * .12, h * .2, w * .78, h * .65),
                         fill=fill, outline=color, width=stroke)
            draw.ellipse((w * .65, h * .03, w * .98, h * .43),
                         fill=fill, outline=color, width=stroke)
            for x in (.22, .38, .6, .72):
                draw.line([(w * x, h * .6), (w * x, h * .97)], fill=color, width=stroke)
            draw.line([(w * .15, h * .4), (0, h * .16)], fill=color, width=stroke)
        elif item["kind"] == "ellipse":
            draw.ellipse((1, 1, w - 1, h - 1), fill=fill, outline=color, width=stroke)
        elif item["kind"] == "line":
            draw.line([(0, h / 2), (w, h / 2)], fill=color, width=stroke)
        else:
            draw.rectangle((1, 1, w - 1, h - 1), fill=fill, outline=color, width=stroke)
        if item["flipX"]:
            shape = ImageOps.mirror(shape)
        # Browser positive rotations are clockwise, Pillow's are counterclockwise.
        shape = shape.rotate(-item["rotation"],
                             resample=Image.Resampling.BICUBIC, expand=True)
        left = round(width * item["x"] / 100 + (w - shape.width) / 2)
        top = round(height * item["y"] / 100 + (h - shape.height) / 2)
        scene.alpha_composite(shape, (left, top))
    output = BytesIO()
    scene.convert("RGB").save(output, format="PNG")
    return output.getvalue()
