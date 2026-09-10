"""Bailian image families share messages while retaining native output controls."""

from app.services.media_ai_io import MediaAIError, MediaInput

MULTIMODAL_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
IMAGE_PATH = "/api/v1/services/aigc/image-generation/generation"
VIDEO_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
IMAGE_SIZES = {"1:1": "1024*1024", "16:9": "1536*864", "9:16": "864*1536",
               "4:3": "1152*864", "3:4": "864*1152"}


def image_payload(model: str, args: dict, media: list[MediaInput]) -> tuple[str, dict]:
    options = dict(args.get("parameters") or {})
    if any(item.kind != "image" for item in media):
        raise MediaAIError("referenceImagesOnly")
    if model.startswith("z-image") and media:
        raise MediaAIError("inputCombination")
    # These modes create additional paid artifacts independently of n.
    if (options.get("result_type") == "series" or options.get("enable_sequential") is True
            or options.get("enable_interleave") is True):
        raise MediaAIError("unsupportedOptions")
    parameters = {"n": 1, **options}
    if model.startswith("kling/"):
        parameters.setdefault("resolution", "1k")
        parameters.setdefault("aspect_ratio", "1:1")
        if "ratio" in args:
            parameters["aspect_ratio"] = args["ratio"]
        if "resolution" in args:
            parameters["resolution"] = args["resolution"]
        if "size" in args:
            raise MediaAIError("unsupportedOptions")
    else:
        sizes = IMAGE_SIZES
        if model in {"qwen-image", "qwen-image-max", "qwen-image-plus"}:
            sizes = {"1:1": "1328*1328", "16:9": "1664*928", "9:16": "928*1664",
                     "4:3": "1472*1104", "3:4": "1104*1472"}
        if model.startswith("vidu/"):
            sizes = ({"1:1": "1024*1024", "16:9": "1920*1088", "9:16": "1088*1920",
                      "4:3": "1024*768", "3:4": "768*1024"} if "/vidu-image" in model else
                     {"1:1": "1024*1024", "16:9": "1376*768", "9:16": "768*1376",
                      "4:3": "1200*896", "3:4": "896*1200"})
        size = args.get("size") or (sizes.get(args["ratio"]) if "ratio" in args else options.get("size", sizes["1:1"]))
        if not size:
            raise MediaAIError("unsupportedOptions")
        parameters["size"] = size
    path = IMAGE_PATH if model.startswith(("kling/", "vidu/")) else MULTIMODAL_PATH
    return path, {
        "model": model,
        "input": {"messages": [{"role": "user", "content": [
            *[{"image": item.data_url} for item in media], {"text": args["prompt"]},
        ]}]},
        "parameters": parameters,
    }
