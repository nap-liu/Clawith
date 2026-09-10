"""Documented native video field mappings; scheduling stays in shared media jobs."""

from app.services.media_ai_io import MediaAIError, MediaInput

# Provider pixel grids, rather than arithmetic rounding, determine valid sizes.
RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16", "3:2", "2:3", "21:9")
PIXVERSE_SIZES = {
    "360P": ("640*360", "640*480", "640*640", "480*640", "360*640", "640*432", "432*640", "640*288"),
    "540P": ("1024*576", "1024*768", "1024*1024", "768*1024", "576*1024", "1024*688", "688*1024", "1024*448"),
    "720P": ("1280*720", "1108*832", "960*960", "832*1108", "720*1280", "1200*800", "800*1200", "1280*560"),
    "1080P": ("1920*1080", "1664*1248", "1440*1440", "1248*1664", "1080*1920", "1776*1184", "1184*1776", "1920*832"),
}
VIDU_SIZES = {
    "540P": PIXVERSE_SIZES["540P"][:5],
    "720P": ("1280*720", "1280*960", "1280*1280", "960*1280", "720*1280"),
    "1080P": ("1920*1080", "1920*1440", "1808*1808", "1440*1920", "1080*1920"),
}


def _pixel_size(table: dict, resolution: str, ratio: str) -> str:
    try:
        return table[resolution][RATIOS.index(ratio)]
    except (KeyError, ValueError, IndexError) as exc:
        raise MediaAIError("unsupportedOptions") from exc


def _references(model: str, media: list[MediaInput]) -> list[dict]:
    frames = model.endswith(("-kf2v", "_start-end2video"))
    ordered = sorted(media, key=lambda item: item.role == "last_frame") if frames else media
    references = []
    for index, item in enumerate(ordered):
        reference_mode = model.endswith(("-r2v", "_reference2video", "-r2v-omni"))
        role = item.role or ("first_frame" if len(media) == 1 and item.kind == "image" and not reference_mode
                             else f"reference_{item.kind}")
        if frames and not item.role:
            role = "first_frame" if index == 0 else "last_frame"
        expected = "image" if role in {"first_frame", "last_frame"} else role.removeprefix("reference_")
        if expected != item.kind:
            raise MediaAIError("inputCombination")
        if model.startswith("kling/"):
            role = {"reference_image": "refer", "reference_video": "feature"}.get(role, role)
        elif model.startswith("pixverse/"):
            role = role if frames else f"{item.kind}_url"
        elif model.startswith("vidu/"):
            role = item.kind
        elif model == "happyhorse-1.0-video-edit":
            role = "video" if item.kind == "video" else role
        elif model == "MiniMax/MiniMax-H3":
            role = {"reference_image": "image_url", "reference_video": "feature",
                    "reference_audio": "driving_audio"}.get(role, role)
        references.append({"type": role, "url": item.data_url})
    return references


def video_payload(model: str, args: dict, media: list[MediaInput]) -> dict:
    parameters = dict(args.get("parameters") or {})
    input_data = {"prompt": args["prompt"]}
    # Native input controls remain available through the normalized parameters bag.
    input_keys = {"negative_prompt"}
    if model.startswith("kling/"):
        input_keys |= {"multi_shot", "shot_type", "multi_prompt", "element_list", "keep_original_sound"}
    for key in input_keys:
        if key in parameters:
            input_data[key] = parameters.pop(key)
    references = _references(model, media)
    if references:
        input_data["media"] = references
    for key in ("duration", "resolution", "size"):
        if key in args:
            parameters[key] = args[key]
    if model == "happyhorse-1.0-video-edit":
        if any(key in parameters or key in args for key in ("duration", "ratio")):
            raise MediaAIError("unsupportedOptions")
        parameters.setdefault("resolution", "720P")
        return {"model": model, "input": input_data, "parameters": parameters}
    parameters.setdefault("duration", 5)
    if model.startswith("kling/"):
        resolution = parameters.pop("resolution", None)
        if resolution:
            parameters["mode"] = {"720P": "std", "1080P": "pro", "4K": "4k"}.get(resolution, resolution)
        parameters.setdefault("mode", "std")
        if "ratio" in args:
            parameters["aspect_ratio"] = args["ratio"]
        elif not references:
            parameters.setdefault("aspect_ratio", "16:9")
    elif model.startswith("pixverse/"):
        size_mode = model.endswith(("-t2v", "-r2v"))
        if size_mode:
            resolution = parameters.pop("resolution", "720P")
            ratio = args.get("ratio", "16:9")
            table = PIXVERSE_SIZES
            if "v5.6" in model:
                table = {**table, **VIDU_SIZES}
            if "size" not in parameters or "ratio" in args or "resolution" in args:
                parameters["size"] = _pixel_size(table, resolution, ratio)
        else:
            parameters.setdefault("resolution", "720P")
            if model.endswith("-omni"):
                parameters["aspect_ratio"] = args.get("ratio", parameters.get("aspect_ratio", "16:9"))
            elif "ratio" in args:
                raise MediaAIError("unsupportedOptions")
    elif model.startswith("vidu/"):
        parameters.setdefault("resolution", "720P")
        if "ratio" in args:
            if model.endswith(("_img2video", "_start-end2video")):
                raise MediaAIError("unsupportedOptions")
            parameters["size"] = _pixel_size(VIDU_SIZES, parameters["resolution"], args["ratio"])
    else:
        parameters.setdefault("resolution", "768P" if model == "MiniMax/MiniMax-H3" else "720P")
        if model.startswith("happyhorse-") and model.endswith("-i2v"):
            if "ratio" in args:
                raise MediaAIError("unsupportedOptions")
        else:
            parameters.setdefault("ratio", "adaptive" if media and not model.startswith("happyhorse-") else "16:9")
            if "ratio" in args:
                parameters["ratio"] = args["ratio"]
    return {"model": model, "input": input_data, "parameters": parameters}
