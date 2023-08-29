import traceback
import json
from docker.errors import ImageNotFound
from typing import List
from supervisely_lib.io.exception_handlers import HandleException, handle_additional_exceptions


class PullErrors:
    class ImageNotFound(HandleException):
        def __init__(self, exception: Exception, stack: List[traceback.FrameSummary]):
            self.code = "d1001"
            self.title = "Docker image not found"
            json_text = exception.args[0].response.text
            default_msg = "no additional info provided"

            try:
                info = json.loads(json_text)
                self.message = info.get("message", default_msg)
            except json.decoder.JSONDecodeError:
                self.message = default_msg

            super().__init__(
                exception,
                stack,
                code=self.code,
                title=self.title,
                message=self.message,
            )


ERRORS_PATTERN = {
    ImageNotFound: {r".*sly\.docker_utils\.docker_pull_if_needed.*": PullErrors.ImageNotFound}
}

handle_exceptions = handle_additional_exceptions(ERRORS_PATTERN)
