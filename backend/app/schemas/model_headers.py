"""HTTP field syntax shared by model configuration and connection probes."""

from typing import Annotated

from pydantic import StringConstraints

HeaderName = Annotated[str, StringConstraints(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")]
HeaderValue = Annotated[str, StringConstraints(pattern=r"^[\t\x20-\x7e]*$")]
ExtraHeaders = dict[HeaderName, HeaderValue]
