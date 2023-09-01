import traceback
import json
from docker.errors import ImageNotFound
from typing import List
from supervisely_lib.io.exception_handlers import HandleException, handle_additional_exceptions


class PullErrors:
    class ImageNotFound(HandleException):
        def __init__(self, exception: Exception, stack: List[traceback.FrameSummary]):
            # TODO: rule for error codes inside side apps/modules
            self.code = 11001
            self.title = "Docker image not found"
            default_msg = str(exception)

            try:
                json_text = exception.args[0].response.text
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
